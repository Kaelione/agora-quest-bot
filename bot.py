import discord
from discord.ext import commands
import os
from datetime import datetime
import random
from flask import Flask
import threading
import psycopg2
from psycopg2 import pool
import requests

# =========================================================
# IA (Groq - gratuit, sans carte bancaire)
# =========================================================
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.3-70b-versatile"

def ask_groq(messages):
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": GROQ_MODEL,
            "messages": messages,
            "max_tokens": 500
        },
        timeout=30
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]

# Stocke les conversations actives : {user_id: [liste de messages]}
active_chats = {}

# =========================================================
# BASE DE DONNÉES (Postgres via Supabase - persistante)
# =========================================================
# IMPORTANT : il faut définir la variable d'environnement DATABASE_URL
# sur Render, avec la chaîne de connexion fournie par Supabase.
DATABASE_URL = os.getenv("DATABASE_URL")

db_pool = psycopg2.pool.SimpleConnectionPool(1, 5, DATABASE_URL)

def get_conn():
    return db_pool.getconn()

def release_conn(conn):
    db_pool.putconn(conn)

def init_db():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                points INTEGER DEFAULT 0,
                daily INTEGER DEFAULT 0,
                date TEXT,
                streak INTEGER DEFAULT 0,
                last_played TEXT
            )
        """)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS streak INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_played TEXT")
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)

init_db()

# =========================================================
# SERVEUR FLASK (garde le process actif / répond aux pings)
# =========================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive"

def run_web():
    app.run(host="0.0.0.0", port=10000)

threading.Thread(target=run_web).start()

# =========================================================
# QUIZ
# =========================================================
questions = [
    {"q": """Est ce une bonne idée de disso?
    A) Oui
    B) ça dépend de qui est loup avec nous
    C) Non
    D) ça dépend de qui est dans la game""",
     "a": "D"},
    {"q": """Quel est la meilleure catégorie de role pour une réflexion totale ?
    A) les roles a info
    B) les roles de protection
    C) les roles passifs
    D) les loups""",
     "a": "C"},
    {"q": """C'est quoi des gp complémentaire ?
    A) deux gp qui s'opposent mais ensemble avance bien
    B) deux gp qui se ressemblent et avance bien ensemble
    C) deux gp très différents qui se gêne l'un l'autre
    D) deux gp qui sont exactement les meme sans impact sur l'autre""",
     "a": "A"}
]

# =========================================================
# BOT DISCORD
# =========================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DAILY_LIMIT = 50

def add_points(user_id, amount):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT points, daily, date FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()

        if row is None:
            points, daily, date = 0, 0, today
            cur.execute(
                "INSERT INTO users (user_id, points, daily, date) VALUES (%s, %s, %s, %s)",
                (user_id, 0, 0, today)
            )
        else:
            points, daily, date = row

        if date != today:
            daily = 0
            date = today

        if daily >= DAILY_LIMIT:
            conn.commit()
            return 0

        if daily + amount > DAILY_LIMIT:
            amount = DAILY_LIMIT - daily

        points += amount
        daily += amount

        cur.execute(
            "UPDATE users SET points = %s, daily = %s, date = %s WHERE user_id = %s",
            (points, daily, date, user_id)
        )
        conn.commit()
        cur.close()
        return amount
    finally:
        release_conn(conn)

def add_direct_points(user_id, amount):
    """Ajoute des points bonus sans les compter dans la limite quotidienne."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET points = points + %s WHERE user_id = %s", (amount, user_id))
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)

STREAK_BONUS = 10          # points bonus donnés à chaque palier
STREAK_MILESTONE = 5       # un palier tous les X jours de streak

def update_streak(user_id):
    """
    Met à jour le streak d'un joueur suite à une bonne réponse au !defi.
    Retourne (streak_actuel, bonus_gagné_cette_fois).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    from datetime import timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT streak, last_played FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()

        if row is None:
            streak, last_played = 0, None
            cur.execute(
                "INSERT INTO users (user_id, points, daily, date, streak, last_played) VALUES (%s, 0, 0, %s, %s, %s)",
                (user_id, today, 0, None)
            )
        else:
            streak, last_played = row

        if last_played == today:
            # a déjà joué aujourd'hui, le streak ne change pas une 2e fois
            cur.close()
            return streak, 0

        if last_played == yesterday:
            streak += 1
        else:
            streak = 1  # streak cassé (ou premier jour) -> on repart à 1

        cur.execute(
            "UPDATE users SET streak = %s, last_played = %s WHERE user_id = %s",
            (streak, today, user_id)
        )
        conn.commit()
        cur.close()

        bonus = 0
        if streak % STREAK_MILESTONE == 0:
            bonus = STREAK_BONUS
            add_direct_points(user_id, bonus)

        return streak, bonus
    finally:
        release_conn(conn)

@bot.event
async def on_ready():
    print(f"{bot.user} est connecté et en ligne !")

@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong !")

@bot.command()
async def score(ctx):
    user_id = str(ctx.author.id)
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT points, daily, date FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()

        if row is None:
            points, daily = 0, 0
            cur.execute(
                "INSERT INTO users (user_id, points, daily, date) VALUES (%s, %s, %s, %s)",
                (user_id, 0, 0, today)
            )
            conn.commit()
        else:
            points, daily, date = row
            if date != today:
                daily = 0
                cur.execute(
                    "UPDATE users SET daily = %s, date = %s WHERE user_id = %s",
                    (0, today, user_id)
                )
                conn.commit()
        cur.close()
    finally:
        release_conn(conn)

    await ctx.send(
        f"🏆 {ctx.author.name}\n"
        f"Points totaux : {points}\n"
        f"Points aujourd'hui : {daily}/{DAILY_LIMIT}"
    )

@bot.command()
async def defi(ctx):
    question = random.choice(questions)
    await ctx.send("🧠 Défi : " + question["q"])

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        msg = await bot.wait_for("message", check=check, timeout=15)
        if msg.content.strip().lower() == question["a"].strip().lower():
            user_id = str(ctx.author.id)
            gained = add_points(user_id, 2)
            streak, bonus = update_streak(user_id)

            reply = f"✅ Bonne réponse ! +{gained} points 🏆\n🔥 Streak : {streak} jour(s) d'affilée"
            if bonus > 0:
                reply += f"\n🎁 Palier atteint ! +{bonus} points bonus !"

            await ctx.send(reply)
        else:
            await ctx.send("❌ Mauvaise réponse.")
    except Exception:
        await ctx.send("⏰ Trop lent !")

@bot.command()
async def streak(ctx):
    user_id = str(ctx.author.id)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT streak, last_played FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
    finally:
        release_conn(conn)

    if row is None or row[0] == 0:
        await ctx.send(f"🔥 {ctx.author.name}, tu n'as pas encore de streak. Fais un !defi aujourd'hui pour commencer !")
        return

    current_streak, last_played = row
    today = datetime.now().strftime("%Y-%m-%d")
    from datetime import timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    if last_played not in (today, yesterday):
        await ctx.send(f"💔 {ctx.author.name}, ton streak est cassé (dernier jour joué trop ancien). Relance-le avec !defi !")
        return

    next_milestone = ((current_streak // STREAK_MILESTONE) + 1) * STREAK_MILESTONE
    remaining = next_milestone - current_streak

    await ctx.send(
        f"🔥 {ctx.author.name}\n"
        f"Streak actuel : {current_streak} jour(s)\n"
        f"Prochain palier ({next_milestone} jours) dans {remaining} jour(s) — récompense : +{STREAK_BONUS} points"
    )

@bot.command()
async def chat(ctx):
    user_id = ctx.author.id
    active_chats[user_id] = [
        {
            "role": "system",
            "content": (
                "Tu es un assistant sympa et détendu qui discute sur un serveur Discord. "
                "Réponds de façon naturelle, concise (pas de pavé), et dans la même langue que l'utilisateur."
            )
        }
    ]
    await ctx.send("💬 Mode conversation activé ! Écris-moi ce que tu veux, je te réponds. Tape `!stopchat` pour arrêter.")

@bot.command()
async def stopchat(ctx):
    user_id = ctx.author.id
    if user_id in active_chats:
        del active_chats[user_id]
        await ctx.send("👋 Conversation terminée !")
    else:
        await ctx.send("Tu n'as pas de conversation active.")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    user_id = message.author.id

    # Si l'utilisateur est en mode chat et que ce n'est pas une commande
    if user_id in active_chats and not message.content.startswith("!"):
        active_chats[user_id].append({"role": "user", "content": message.content})
        async with message.channel.typing():
            try:
                reply = ask_groq(active_chats[user_id])
                active_chats[user_id].append({"role": "assistant", "content": reply})
                await message.channel.send(reply[:2000])  # limite Discord = 2000 caractères
            except Exception as e:
                await message.channel.send("⚠️ Erreur avec l'IA, réessaie dans un instant.")
                print(f"Erreur Groq: {e}")

    await bot.process_commands(message)

bot.run(os.getenv("TOKEN"))
