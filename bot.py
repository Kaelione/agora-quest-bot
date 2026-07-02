import discord
from discord import app_commands
from discord.ext import commands, tasks
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
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_points INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS best_streak INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS best_month_points INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS monthly_top1_count INTEGER DEFAULT 0")
        # Rattrape best_streak pour les joueurs qui avaient déjà une streak en cours
        # avant l'ajout de cette colonne
        cur.execute("UPDATE users SET best_streak = streak WHERE best_streak = 0 AND streak > 0")
        # Récupère l'historique existant : comme aucun reset n'a encore eu lieu,
        # "points" représente déjà le total depuis le début pour l'instant.
        cur.execute("UPDATE users SET total_points = points WHERE total_points = 0 AND points > 0")

        # Table pour suivre les infos système (comme le dernier reset mensuel)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # On initialise le mois courant SANS reset si c'est la 1ère fois qu'on lance ce système
        # (évite de remettre tout le monde à 0 dès le déploiement de cette fonctionnalité)
        cur.execute(
            "INSERT INTO meta (key, value) VALUES ('last_reset_month', %s) ON CONFLICT (key) DO NOTHING",
            (datetime.now().strftime("%Y-%m"),)
        )

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
    {
        "q": "Est ce une bonne idée de disso ?",
        "options": {
            "A": "Oui",
            "B": "ça dépend de qui est loup avec nous",
            "C": "Non",
            "D": "ça dépend de qui est dans la game"
        },
        "a": "D", "difficulty": "moyen", "points": 2
    },
    {
        "q": "Quel est la meilleure catégorie de role pour une réflexion totale ?",
        "options": {
            "A": "les roles a info",
            "B": "les roles de protection",
            "C": "les roles passifs",
            "D": "les loups"
        },
        "a": "C", "difficulty": "difficile", "points": 3
    },
    {
        "q": "C'est quoi des gp complémentaires ?",
        "options": {
            "A": "deux gp qui s'opposent mais ensemble avancent bien",
            "B": "deux gp qui se ressemblent et avancent bien ensemble",
            "C": "deux gp très différents qui se gênent l'un l'autre",
            "D": "deux gp qui sont exactement les mêmes, sans impact sur l'autre"
        },
        "a": "A", "difficulty": "facile", "points": 1
    },
    {
        "q": "Dans la technique du \"2 safes 1 loup\" au tour décisif (TD), combien de joueurs de chaque camp restent en jeu ?",
        "options": {
            "A": "1 safe et 1 loup",
            "B": "2 safes et 1 loup",
            "C": "2 loups et 1 safe",
            "D": "3 safes"
        },
        "a": "B", "difficulty": "facile", "points": 1
    },
    {
        "q": "Pourquoi le maire doit-il laisser le vote temporairement en égalité pendant le tour décisif ?",
        "options": {
            "A": "Pour perdre du temps sans raison",
            "B": "Pour gagner du temps, se placer au centre de l'action et mettre le loup dans une situation critique",
            "C": "Pour éviter d'avoir à voter",
            "D": "Pour laisser le safe décider à sa place"
        },
        "a": "B", "difficulty": "moyen", "points": 2
    },
    {
        "q": "Pourquoi le maire doit-il tuer immédiatement après avoir utilisé la phrase clé (\"oui merci, j'ai win\") ?",
        "options": {
            "A": "Parce que reporter le choix laisse le loup se recalibrer, alors que tuer immédiatement exploite sa réaction spontanée",
            "B": "Parce que c'est une règle du jeu obligatoire",
            "C": "Parce que les autres joueurs préfèrent que ça aille vite",
            "D": "Parce que le loup l'exige"
        },
        "a": "A", "difficulty": "difficile", "points": 3
    }
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
            "UPDATE users SET points = %s, daily = %s, date = %s, total_points = total_points + %s WHERE user_id = %s",
            (points, daily, date, amount, user_id)
        )
        conn.commit()
        cur.close()
        return amount
    finally:
        release_conn(conn)

def add_direct_points(user_id, amount):
    """Ajoute des points bonus sans les compter dans la limite quotidienne (mais comptés dans le total historique)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET points = points + %s, total_points = total_points + %s WHERE user_id = %s",
            (amount, amount, user_id)
        )
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)

STREAK_MILESTONE = 5  # un palier tous les X jours de streak
STREAK_BONUS_CAP = 50  # plafond du bonus par palier

def get_streak_bonus(streak):
    """
    Calcule le bonus de points pour un palier de streak donné.
    Paliers : 5j->5, 10j->7, 15j->10, 20j->15, 25j->20, 30j->25,
    puis +5 par palier supplémentaire, plafonné à 50.
    """
    if streak % STREAK_MILESTONE != 0:
        return 0

    n = streak // STREAK_MILESTONE  # numéro du palier (1, 2, 3...)
    explicit_bonuses = {1: 5, 2: 7, 3: 10, 4: 15, 5: 20, 6: 25}

    if n in explicit_bonuses:
        bonus = explicit_bonuses[n]
    else:
        bonus = 25 + (n - 6) * 5

    return min(bonus, STREAK_BONUS_CAP)

def update_streak(user_id):
    """
    Met à jour le streak d'un joueur suite à une bonne réponse au /defi.
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
            "UPDATE users SET streak = %s, last_played = %s, best_streak = GREATEST(best_streak, %s) WHERE user_id = %s",
            (streak, today, streak, user_id)
        )
        conn.commit()
        cur.close()

        bonus = get_streak_bonus(streak)
        if bonus > 0:
            add_direct_points(user_id, bonus)

        return streak, bonus
    finally:
        release_conn(conn)

class QuizView(discord.ui.View):
    def __init__(self, question, author_id):
        super().__init__(timeout=20)
        self.question = question
        self.author_id = author_id
        self.answered = False
        self.message = None

        for letter in ["A", "B", "C", "D"]:
            if letter in question["options"]:
                button = discord.ui.Button(label=letter, style=discord.ButtonStyle.primary)
                button.callback = self.make_callback(letter)
                self.add_item(button)

    def make_callback(self, letter):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author_id:
                await interaction.response.send_message("Ce n'est pas ton défi !", ephemeral=True)
                return
            if self.answered:
                await interaction.response.defer()
                return

            self.answered = True
            for child in self.children:
                child.disabled = True

            user_id = str(self.author_id)
            embed = interaction.message.embeds[0]

            if letter == self.question["a"]:
                gained = add_points(user_id, self.question["points"])
                streak, bonus = update_streak(user_id)
                result = f"✅ Bonne réponse ! +{gained} points 🏆\n🔥 Streak : {streak} jour(s) d'affilée"
                if bonus > 0:
                    result += f"\n🎁 Palier atteint ! +{bonus} points bonus !"
                embed.color = discord.Color.green()
            else:
                bonne = self.question["a"]
                result = f"❌ Mauvaise réponse. La bonne réponse était **{bonne}) {self.question['options'][bonne]}**"
                embed.color = discord.Color.red()

            embed.description = result
            await interaction.response.edit_message(embed=embed, view=self)
        return callback

    async def on_timeout(self):
        if self.answered or self.message is None:
            return
        for child in self.children:
            child.disabled = True
        try:
            embed = self.message.embeds[0]
            embed.description = "⏰ Trop lent, personne n'a répondu à temps !"
            embed.color = discord.Color.orange()
            await self.message.edit(embed=embed, view=self)
        except Exception:
            pass

@tasks.loop(hours=6)
async def check_monthly_reset():
    current_month = datetime.now().strftime("%Y-%m")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM meta WHERE key = 'last_reset_month'")
        row = cur.fetchone()
        last_reset = row[0] if row else None

        if last_reset != current_month:
            # Compte comme "top 1" tous les joueurs à égalité en tête (s'il y a égalité)
            cur.execute("SELECT MAX(points) FROM users")
            max_points = cur.fetchone()[0]
            if max_points and max_points > 0:
                cur.execute(
                    "UPDATE users SET monthly_top1_count = monthly_top1_count + 1 WHERE points = %s",
                    (max_points,)
                )

            cur.execute("UPDATE users SET best_month_points = GREATEST(best_month_points, points)")
            cur.execute("UPDATE users SET points = 0")
            cur.execute(
                "INSERT INTO meta (key, value) VALUES ('last_reset_month', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = %s",
                (current_month, current_month)
            )
            conn.commit()
            print(f"🔄 Reset mensuel effectué pour {current_month}")
        cur.close()
    finally:
        release_conn(conn)

@bot.event
async def on_ready():
    await bot.tree.sync()
    if not check_monthly_reset.is_running():
        check_monthly_reset.start()
    print(f"{bot.user} est connecté et en ligne !")

@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong !")

@bot.tree.command(name="score", description="Affiche tes points et ta progression du jour")
async def score(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
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

    await interaction.response.send_message(
        f"🏆 {interaction.user.name}\n"
        f"Points totaux : {points}\n"
        f"Points aujourd'hui : {daily}/{DAILY_LIMIT}"
    )

@bot.tree.command(name="defi", description="Répond à une question quiz du serveur")
async def defi(interaction: discord.Interaction):
    question = random.choice(questions)

    options_text = "\n".join(
        f"**{letter})** {text}" for letter, text in question["options"].items()
    )

    embed = discord.Embed(
        title=f"🧠 Défi [{question['difficulty'].capitalize()} — {question['points']} pt(s)]",
        description=f"{question['q']}\n\n{options_text}",
        color=discord.Color.blurple()
    )

    view = QuizView(question, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()

@bot.tree.command(name="streak", description="Affiche ton streak actuel et le prochain palier")
async def streak(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT streak, last_played FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
    finally:
        release_conn(conn)

    if row is None or row[0] == 0:
        await interaction.response.send_message(f"🔥 {interaction.user.name}, tu n'as pas encore de streak. Fais un /defi aujourd'hui pour commencer !")
        return

    current_streak, last_played = row
    today = datetime.now().strftime("%Y-%m-%d")
    from datetime import timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    if last_played not in (today, yesterday):
        await interaction.response.send_message(f"💔 {interaction.user.name}, ton streak est cassé (dernier jour joué trop ancien). Relance-le avec /defi !")
        return

    next_milestone = ((current_streak // STREAK_MILESTONE) + 1) * STREAK_MILESTONE
    remaining = next_milestone - current_streak

    await interaction.response.send_message(
        f"🔥 {interaction.user.name}\n"
        f"Streak actuel : {current_streak} jour(s)\n"
        f"Prochain palier ({next_milestone} jours) dans {remaining} jour(s) — récompense : +{get_streak_bonus(next_milestone)} points"
    )

@bot.tree.command(name="chat", description="Active le mode conversation avec l'IA")
async def chat(interaction: discord.Interaction):
    user_id = interaction.user.id
    active_chats[user_id] = [
        {
            "role": "system",
            "content": (
                "Tu es un assistant sympa et détendu qui discute sur un serveur Discord. "
                "Réponds de façon naturelle, concise (pas de pavé), et dans la même langue que l'utilisateur."
            )
        }
    ]
    await interaction.response.send_message("💬 Mode conversation activé ! Écris-moi ce que tu veux, je te réponds. Tape `/stopchat` pour arrêter.")

@bot.tree.command(name="stopchat", description="Désactive le mode conversation avec l'IA")
async def stopchat(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in active_chats:
        del active_chats[user_id]
        await interaction.response.send_message("👋 Conversation terminée !")
    else:
        await interaction.response.send_message("Tu n'as pas de conversation active.", ephemeral=True)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    user_id = message.author.id

    # Si l'utilisateur est en mode chat et que ce n'est pas une commande (!ping)
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

@bot.tree.command(name="classement", description="Affiche le classement du serveur (points et streak)")
async def classement(interaction: discord.Interaction):
    await interaction.response.defer()

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, points FROM users WHERE points > 0 ORDER BY points DESC LIMIT 10")
        top_points = cur.fetchall()
        cur.execute("SELECT user_id, streak FROM users WHERE streak > 0 ORDER BY streak DESC LIMIT 10")
        top_streak = cur.fetchall()
        cur.close()
    finally:
        release_conn(conn)

    medals = ["🥇", "🥈", "🥉"]

    async def format_leaderboard(rows, label, emoji, suffix):
        if not rows:
            return f"{emoji} **{label}**\nPersonne pour l'instant."
        lines = [f"{emoji} **{label}**"]
        for i, (user_id, value) in enumerate(rows):
            try:
                user = await bot.fetch_user(int(user_id))
                name = user.name
            except Exception:
                name = f"Utilisateur {user_id}"
            rank = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{rank} {name} — {value} {suffix}")
        return "\n".join(lines)

    points_text = await format_leaderboard(top_points, "Classement Points", "🏆", "pts")
    streak_text = await format_leaderboard(top_streak, "Classement Streak", "🔥", "jour(s)")

    await interaction.followup.send(f"{points_text}\n\n{streak_text}")

@bot.tree.command(name="stat", description="Affiche les statistiques d'un joueur (toi-même par défaut)")
@app_commands.describe(membre="Le membre à consulter (laisse vide pour toi-même)")
async def stat(interaction: discord.Interaction, membre: discord.Member = None):
    target = membre or interaction.user
    user_id = str(target.id)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT total_points, points, streak, best_streak, best_month_points, monthly_top1_count "
            "FROM users WHERE user_id = %s",
            (user_id,)
        )
        row = cur.fetchone()
        cur.close()
    finally:
        release_conn(conn)

    if row is None:
        total_points, month_points, streak, best_streak, best_month_points, top1_count = 0, 0, 0, 0, 0, 0
    else:
        total_points, month_points, streak, best_streak, best_month_points, top1_count = row

    joined_str = target.joined_at.strftime("%d/%m/%Y") if target.joined_at else "Inconnue"
    roles = [role.mention for role in target.roles if role.name != "@everyone"]
    roles_str = ", ".join(roles) if roles else "Aucun rôle"

    embed = discord.Embed(
        title=f"📊 Statistiques de {target.display_name}",
        color=discord.Color.gold()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="📅 A rejoint le serveur", value=joined_str, inline=False)
    embed.add_field(name="🏆 Points totaux (depuis le début)", value=str(total_points), inline=True)
    embed.add_field(name="📆 Points ce mois-ci", value=str(month_points), inline=True)
    embed.add_field(name="🌟 Meilleur mois", value=str(best_month_points), inline=True)
    embed.add_field(name="🔥 Streak actuelle", value=f"{streak} jour(s)", inline=True)
    embed.add_field(name="🚀 Meilleure streak", value=f"{best_streak} jour(s)", inline=True)
    embed.add_field(name="👑 Fois classé n°1 (mensuel)", value=str(top1_count), inline=True)
    embed.add_field(name="🎭 Rôles", value=roles_str, inline=False)

    await interaction.response.send_message(embed=embed)

bot.run(os.getenv("TOKEN"))
