import discord
from discord.ext import commands
import json
import os
from datetime import datetime
import random
from flask import Flask
import threading
import sqlite3
conn = sqlite3.connect("points.db")
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    points INTEGER DEFAULT 0,
    daily INTEGER DEFAULT 0,
    date TEXT
)
""")

conn.commit()

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive"

def run_web():
    app.run(host="0.0.0.0", port=10000)

threading.Thread(target=run_web).start()

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

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

DAILY_LIMIT = 50


def add_points(user_id, amount):
    today = datetime.now().strftime("%Y-%m-%d")

    c.execute("SELECT points, daily, date FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()

    if row is None:
        points, daily, date = 0, 0, today
        c.execute("INSERT INTO users VALUES (?, ?, ?, ?)", (user_id, 0, 0, today))
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

    c.execute("""
        UPDATE users
        SET points = ?, daily = ?, date = ?
        WHERE user_id = ?
    """, (points, daily, date, user_id))

    conn.commit()

    return amount


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

    c.execute("SELECT points, daily, date FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()

    if row is None:
        points, daily = 0, 0
        c.execute("INSERT INTO users VALUES (?, ?, ?, ?)", (user_id, 0, 0, today))
        conn.commit()
    else:
        points, daily, date = row
        if date != today:
            daily = 0
            c.execute("UPDATE users SET daily = ?, date = ? WHERE user_id = ?", (0, today, user_id))
            conn.commit()

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
            gained = add_points(str(ctx.author.id), 2)
            await ctx.send(f"✅ Bonne réponse ! +{gained} points 🏆")
        else:
            await ctx.send("❌ Mauvaise réponse.")

    except:
        await ctx.send("⏰ Trop lent !")

bot.run(os.getenv("TOKEN"))
