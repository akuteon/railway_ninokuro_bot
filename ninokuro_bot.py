# discord.pyライブラリをインポート（Bot機能を使うため）
from discord.ext import commands
from datetime import datetime, timedelta
from urllib.parse import urljoin
from dotenv import load_dotenv
from supabase import create_client, Client
from dateutil import parser
from flask import Flask, redirect
from pytz import timezone
import threading
import discord
import os
import asyncio
import signal
import time


# .env環境ファイル読み込み
load_dotenv()
TOKEN = os.environ["TOKEN"]

# タイムゾーン
jst = timezone('Asia/Tokyo')

class CustomBot(commands.Bot):
    async def async_cleanup(self):
        print("🧹 Bot終了前のクリーンアップ処理を実行中...")
        # ここにDB切断やログ出力などを記述

    async def close(self):
        await self.async_cleanup()
        await super().close()

# Supabase接続処理
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(url, key)

# Botがリアクションやメッセージ内容にアクセスできるようにする設定
intents = discord.Intents.default()
intents.message_content = True  # メッセージ本文へのアクセスを許可
intents.reactions = True        # リアクション（スタンプ）へのアクセスを許可
# Botのプレフィックスとインテントを指定してインスタンスを作成
bot = CustomBot(command_prefix="!", intents=intents)

# 別のWebアプリのトップURL
if os.getenv("RENDER") == "true":
    WEB_APP_URL = "https://ninokuro-party.onrender.com/"
else:
    WEB_APP_URL = "http://127.0.0.1:5000"

# 日本語の曜日リスト（0=月曜〜6=日曜）
weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]

# スタンプの意味（収集時にも使う）
reaction_labels = {
    "⭕": "行ける",
    "❌": "行けない",
    "🤷": "ドタキャンの可能性はあるけど行きたいので組み込んで",
    "⏰": "時間の調整あればいける"
}

# Botが起動したときに呼ばれるイベントハンドラ
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")  # コンソールにBotのログイン情報を表示

# 出席確認メッセージを送信し、Bot自身がスタンプを押すコマンド
@bot.command()
async def start_week(ctx):
    today = datetime.now(jst)
    print(today)
    today_weekday = today.weekday()  # 0=月曜, 6=日曜

    # 翌週の月曜を基準にする（月曜なら今週）
    if today_weekday == 0:
        base_date = today
    else:
        days_until_next_monday = (7 - today_weekday) % 7
        base_date = today + timedelta(days=days_until_next_monday)

    week_start = base_date.date()
    server_id = str(ctx.guild.id)

    # 重複実行チェック（同じ週が既に存在するか）
    existing = supabase.table("weekly_attendance") \
        .select("id") \
        .eq("server_id", server_id) \
        .eq("week_start", week_start.isoformat()) \
        .execute()

    if existing.data:
        await ctx.send("⚠️ 今週の出欠記録はすでに開始されています。再実行はできません。")
        return

    # 凡例メッセージ送信
    legend_text = (
        "⭕：行ける\n"
        "❌：行けない\n"
        "🤷：ドタキャンの可能性はあるけど行きたいので組み込んで\n"
        "⏰：時間の調整あればいける"
    )
    await ctx.send(legend_text)

    # meta構築（曜日ごとのメッセージID・日付・responses）
    meta = {}

    for i in range(7):
        target_date = base_date + timedelta(days=i)
        date_str = target_date.strftime("%Y/%m/%d")
        weekday_name = weekday_jp[i]

        message_text = f" {date_str}（{weekday_name}）"
        msg = await ctx.send(message_text)

        for emoji in reaction_labels:
            await msg.add_reaction(emoji)

        meta[date_str] = {
            "weekday": weekday_name,
            "message_id": str(msg.id),
            "responses": {
                "行ける": [],
                "行けない": [],
                "ドタキャンの可能性はあるけど行きたいので組み込んで": [],
                "時間の調整あればいける": []
            }
        }

    # ✅ upload_attendance() に保存処理を委譲
    success = await upload_attendance(ctx, meta)
    if success:
        await ctx.send("✅ 翌週の出席確認メッセージを曜日ごとに送信しました！スタンプを押してください。")


# 重複実行時でも強制実行するためのコマンド
@bot.command()
async def initialize_week(ctx):
    server_id = str(ctx.guild.id)
    success = initialize_attendance_check_data(server_id)

    if success:
        await ctx.send("🧹 最新週の出欠記録を初期化しました。再度!start_weekを実行できます。")
    else:
        await ctx.send("⚠️ 初期化対象のデータが見つかりませんでした。")
 
# 出席情報を収集するコマンド
@bot.command()
async def collect_week(ctx):
    print(f"[COMMAND] collect_week triggered by {ctx.author} at {datetime.now(jst)}")

    server_id = str(ctx.guild.id)
    await ctx.send("データ収集中...")

    # Supabaseから最新の週のmetaを取得
    response = supabase.table("weekly_attendance") \
        .select("meta") \
        .eq("server_id", server_id) \
        .order("week_start", desc=True) \
        .limit(1) \
        .execute()

    if not response.data:
        await ctx.send("⚠️ 出席メッセージ情報が見つかりません。事前に!start_weekが実行されているか確認してください。")
        return

    attendance_messages = response.data[0]["meta"]
    attendance_data = {}

    # メッセージ取得（並列）
    fetched_messages = await fetch_messages_parallel(ctx.channel, attendance_messages)

    async def get_users_for_reaction(reaction):
        emoji = str(reaction.emoji)
        if emoji not in reaction_labels:
            return None

        label = reaction_labels[emoji]
        users = []
        async for user in reaction.users():
            if not user.bot:
                users.append(user.name)
        return label, users

    for date_str, msg_or_exc in fetched_messages.items():
        info = attendance_messages[date_str]
        weekday = info["weekday"]

        if isinstance(msg_or_exc, Exception):
            continue

        msg = msg_or_exc

        attendance_data[date_str] = {
            "weekday": weekday,
            "responses": {label: [] for label in reaction_labels.values()}
        }

        # 並列でリアクションユーザー取得
        tasks = [get_users_for_reaction(reaction) for reaction in msg.reactions]
        results = await asyncio.gather(*tasks)

        for result in results:
            if result is None:
                continue
            label, users = result
            attendance_data[date_str]["responses"][label].extend(users)

    if not attendance_data:
        await ctx.send("⚠️ 出席メッセージが1件もありません。")
        return

    success = await upload_attendance(ctx, attendance_data)
    if success:
        view_url = urljoin(WEB_APP_URL + "/from_discord/", server_id)
        await ctx.send(f"✅ 完了しました！こちらからアクセスできます：\n{view_url}")

# メッセージ取得を並列化する関数
async def fetch_messages_parallel(channel, attendance_messages):
    tasks = []
    weekday_keys = []

    for weekday, info in attendance_messages.items():
        # message_id が存在しない場合はスキップ
        if "message_id" not in info:
            continue

        message_id = info["message_id"]
        weekday_keys.append(weekday)
        tasks.append(channel.fetch_message(message_id))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return dict(zip(weekday_keys, results))

# 出席情報をアップロード
async def upload_attendance(ctx, attendance_data):
    server_id = str(ctx.guild.id)

    # ✅ 日付順に並べ替え（キーが日付なので直接ソート可能）
    sorted_attendance = dict(
        sorted(
            attendance_data.items(),
            key=lambda item: parser.parse(item[0])  # item[0] = date_str
        )
    )

    # 週の開始日を推定（最初の曜日のdateを使う）
    try:
        first_date_str = next(iter(sorted_attendance))
        if not first_date_str:
            raise ValueError("dateフィールドが見つかりません")
        week_start = parser.parse(first_date_str).date()

    except Exception as e:
        await ctx.send(f"⚠️ 日付の解析に失敗しました: {e}")
        return False

    # Supabaseに保存
    try:
        # 既存レコードチェック
        existing = supabase.table("weekly_attendance") \
            .select("*") \
            .eq("server_id", server_id) \
            .eq("week_start", week_start.isoformat()) \
            .execute()
        
        if existing.data:
            # レコードが存在する → responsesのみ更新
            record_id = existing.data[0]["id"]
            existing_meta = existing.data[0]["meta"]

            # responsesだけ上書き
            for date_str, new_day_data in sorted_attendance.items():
                if date_str in existing_meta:
                    existing_meta[date_str]["responses"] = new_day_data["responses"]

            # 更新
            response = supabase.table("weekly_attendance").update({
                "meta": existing_meta
            }).eq("id", record_id).execute()

        else:
            # レコードが存在しない → insert
            response = supabase.table("weekly_attendance").insert({
                "server_id": server_id,
                "week_start": week_start.isoformat(),
                "meta": sorted_attendance
            }).execute()

        return True

    except Exception as e:
        await ctx.send(f"❌ 保存に失敗しました: {e}")
        return False

# 出席情報の初期化
def initialize_attendance_check_data(server_id):
    # Supabaseから最新週のレコード取得
    response = supabase.table("weekly_attendance") \
        .select("id") \
        .eq("server_id", server_id) \
        .order("week_start", desc=True) \
        .limit(1) \
        .execute()

    if not response.data:
        return False  # 初期化失敗（削除対象なし）

    record_id = response.data[0]["id"]

    # レコードを完全に削除
    supabase.table("weekly_attendance") \
        .delete() \
        .eq("id", record_id) \
        .execute()

    return True


# Flaskで外部から定期的にPINGを飛ばすことによりRenderがスリープにならないようにする
app = Flask(__name__)
print("[FLASK] Flask app initialized")

bot_started = False
bot_lock = threading.Lock()

@app.route('/')
def index():
    status = "起動済み" if bot_started else "未起動"
    print(f"[DEBUG] bot_started initial = {bot_started}")
    return f"""
    <html>
        <body>
            <h1>Bot 状態: {status}</h1>
            <form action="/start-bot" method="post">
                <button type="submit">Botを起動する</button>
            </form>
        </body>
    </html>
    """

@app.route('/start-bot', methods=['POST'])
def start_bot_route():
    print("[DEBUG] /start-bot route called")
    global bot_started
    if not bot_started:
        with bot_lock:
            if not bot_started:
                print("[BOT] Triggered by /start-bot")
                threading.Thread(target=run_bot_forever, daemon=True).start()
                bot_started = True
    return redirect('/')

@app.route('/health')
def health_check():
    return "alive"

# Renderでの終了時にBOTのスレッドを終了する。
shutdown_event = threading.Event()

def handle_sigterm(signum, frame):
    print("🛑 SIGTERM received, setting shutdown flag")
    shutdown_event.set()

signal.signal(signal.SIGTERM, handle_sigterm)

async def start_bot():
    await bot.start(TOKEN)

def run_bot_forever():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def bot_runner():
        try:
            print("[BOT] Starting bot.run()")
            await bot.start(TOKEN)
        except Exception as e:
            print(f"[BOT ERROR] {e}")
        finally:
            print("[BOT] Closing bot...")
            await bot.close()

    task = loop.create_task(bot_runner())

    def shutdown_watcher():
        shutdown_event.wait()
        print("[BOT] Shutdown event detected, cancelling bot task...")
        task.cancel()

    threading.Thread(target=shutdown_watcher, daemon=True).start()

    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        print("[BOT] Bot task cancelled")
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


print(f"[ENV] RENDER = {os.getenv('RENDER')}")

if os.getenv("RENDER") != "true":
    bot.run(TOKEN)
    print(f"[BOT START] 起動しました: {datetime.now(jst)}")
