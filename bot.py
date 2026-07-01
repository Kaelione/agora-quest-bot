import discord
from discord.ext import commands
import json
import os
from datetime import datetime
import random
from flask import Flask
import threading
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive"

def run_web():
    app.run(host="0.0.0.0", port=10000)

threading.Thread(target=run_web).start()

questions = [
    {"q": "Combien font 2 + 2 ?", "a": "4"},
    {"q": "Quel est le contraire de jour ?", "a": "nuit"},
    {"q": "Combien de lettres dans 'loup' ?", "a": "4"}
]

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

POINTS_FILE = "points.json"
DAILY_LIMIT = 50


def load_points():
    if os.path.exists(POINTS_FILE):
        with open(POINTS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_points(data):
    with open(POINTS_FILE, "w") as f:
        json.dump(data, f)


points = load_points()


def add_points(user_id, amount):
    today = datetime.now().strftime("%Y-%m-%d")

    if user_id not in points:
        points[user_id] = {
            "points": 0,
            "daily": 0,
            "date": today
        }

    if points[user_id]["date"] != today:
        points[user_id]["daily"] = 0
        points[user_id]["date"] = today

    if points[user_id]["daily"] >= DAILY_LIMIT:
        return 0

    if points[user_id]["daily"] + amount > DAILY_LIMIT:
        amount = DAILY_LIMIT - points[user_id]["daily"]

    points[user_id]["points"] += amount
    points[user_id]["daily"] += amount

    save_points(points)
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

    if user_id not in points:
        points[user_id] = {
            "points": 0,
            "daily": 0,
            "date": today
        }
    else:
        if points[user_id]["date"] != today:
            points[user_id]["daily"] = 0
            points[user_id]["date"] = today

    await ctx.send(
        f"🏆 {ctx.author.name}\n"
        f"Points totaux : {points[user_id]['points']}\n"
        f"Points aujourd'hui : {points[user_id]['daily']}/50"
    )

@bot.command()
async def defi(ctx):
    question = random.choice(questions)

    await ctx.send("🧠 Défi : " + question["q"])

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        msg = await bot.wait_for("message", check=check, timeout=15)

        if msg.content.lower() == question["a"]:
            gained = add_points(str(ctx.author.id), 2)
            await ctx.send(f"✅ Bonne réponse ! +{gained} points 🏆")
        else:
            await ctx.send("❌ Mauvaise réponse.")

    except:
        await ctx.send("⏰ Trop lent !")

bot.run(os.getenv("TOKEN"))
