import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
import re
from datetime import datetime, timedelta
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

MAX_MEMORY_MESSAGES = 30  # nombre de messages (hors system) conservés par utilisateur

def load_chat_memory(user_id):
    """Charge l'historique de conversation persistant d'un utilisateur (liste de messages, sans le system prompt)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT messages FROM chat_memory WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
    finally:
        release_conn(conn)

    if row is None or row[0] is None:
        return []
    try:
        return json.loads(row[0])
    except Exception:
        return []

def save_chat_memory(user_id, messages):
    """Sauvegarde l'historique de conversation (hors system prompt), tronqué aux derniers messages."""
    trimmed = messages[-MAX_MEMORY_MESSAGES:]
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_memory (user_id, messages) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET messages = %s",
            (user_id, json.dumps(trimmed), json.dumps(trimmed))
        )
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)

SHARED_MEMORY_LIMIT = 40  # nombre d'entrées partagées conservées au total (toutes personnes confondues)

def log_shared_memory(author_name, content):
    """Enregistre ce qu'un joueur a dit, accessible ensuite à tous les autres joueurs."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO shared_memory (author_name, content) VALUES (%s, %s)",
            (author_name, content)
        )
        # Garde uniquement les N entrées les plus récentes pour ne pas grossir indéfiniment
        cur.execute("""
            DELETE FROM shared_memory
            WHERE id NOT IN (
                SELECT id FROM shared_memory ORDER BY created_at DESC LIMIT %s
            )
        """, (SHARED_MEMORY_LIMIT,))
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)

def get_shared_memory_context(limit=20):
    """Retourne un texte listant ce que les joueurs ont dit récemment, avec leur nom."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT author_name, content FROM shared_memory ORDER BY created_at DESC LIMIT %s",
            (limit,)
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        release_conn(conn)

    if not rows:
        return ""

    lines = [f"[{author}] : {content}" for author, content in reversed(rows)]
    return "\n".join(lines)

DIFFICULTY_POINTS = {"facile": 1, "moyen": 2, "difficile": 3}

# Mots/pseudos à ne jamais laisser passer dans les questions générées automatiquement
FORBIDDEN_TERMS = ["sangsue"]

def sanitize_text(text):
    """Retire les pseudos/mots interdits d'un texte (insensible à la casse)."""
    import re
    for term in FORBIDDEN_TERMS:
        text = re.sub(re.escape(term), "[joueur]", text, flags=re.IGNORECASE)
    return text

def generate_questions_from_text(source_text, source_label=""):
    """Demande à Groq de générer des questions QCM à partir d'un texte, retourne une liste de dicts."""
    system_prompt = (
        "Tu es un générateur de questions de quiz pour un serveur Discord Loup-Garou (jeu de rôles/stratégie). "
        "À partir du texte fourni, génère entre 1 et 3 questions à choix multiples (QCM) pertinentes, "
        "qui testent la compréhension du contenu. "
        "Réponds STRICTEMENT avec un tableau JSON, sans texte autour, sans balises markdown, au format exact :\n"
        '[{"q": "texte de la question", "options": {"A": "...", "B": "...", "C": "...", "D": "..."}, '
        '"a": "A", "difficulty": "facile", "theme": "stratégie"}]\n'
        'Le champ "a" doit être la lettre de la bonne réponse. '
        'Le champ "difficulty" doit être "facile", "moyen" ou "difficile" selon la complexité de la question. '
        'Le champ "theme" doit être un court mot-clé (1 à 2 mots) résumant le sujet de la question '
        '(ex: "stratégie", "rôles", "vote", "vocabulaire").'
    )

    try:
        raw = ask_groq([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": sanitize_text(source_text)[:4000]}
        ])
        cleaned = raw.strip()
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
        # Si le modèle a ajouté du texte avant/après le JSON, on extrait juste le tableau
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start:end + 1]
        parsed = json.loads(cleaned)
    except Exception as e:
        print(f"⚠️ Erreur génération questions ({source_label}): {e}")
        print(f"⚠️ Réponse brute de l'IA : {locals().get('raw', 'N/A')[:500]}")
        return []

    results = []
    for item in parsed:
        try:
            difficulty = item.get("difficulty", "moyen").lower()
            if difficulty not in DIFFICULTY_POINTS:
                difficulty = "moyen"
            q_text = sanitize_text(item["q"])
            options = {k: sanitize_text(v) for k, v in item["options"].items()}
            theme = sanitize_text(item.get("theme", "général")).strip() or "général"
            results.append({
                "q": q_text,
                "options": options,
                "a": item["a"].upper(),
                "difficulty": difficulty,
                "points": DIFFICULTY_POINTS[difficulty],
                "theme": theme
            })
        except (KeyError, AttributeError):
            continue

    return results

def save_questions_to_db(question_list, source=""):
    """Enregistre une liste de questions générées dans la banque, en ignorant les doublons."""
    saved = 0
    conn = get_conn()
    try:
        cur = conn.cursor()
        for q in question_list:
            try:
                opts = q["options"]
                cur.execute(
                    "INSERT INTO questions (q, option_a, option_b, option_c, option_d, correct, difficulty, points, theme, source) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (q) DO NOTHING",
                    (q["q"], opts["A"], opts["B"], opts["C"], opts["D"], q["a"], q["difficulty"], q["points"], q.get("theme", "général"), source)
                )
                if cur.rowcount > 0:
                    saved += 1
            except Exception as e:
                print(f"Erreur sauvegarde question: {e}")
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)
    return saved

async def generate_questions_from_forum(forum_channel, limit=10):
    """Parcourt les threads d'un forum, génère des questions via IA pour les threads pas encore traités."""
    total_saved = 0
    threads_scanned = 0

    all_threads = list(forum_channel.threads)
    try:
        async for archived in forum_channel.archived_threads(limit=limit):
            all_threads.append(archived)
    except Exception:
        pass

    conn = get_conn()
    try:
        cur = conn.cursor()
        for thread in all_threads[:limit]:
            thread_id = str(thread.id)
            cur.execute("SELECT 1 FROM processed_threads WHERE thread_id = %s", (thread_id,))
            if cur.fetchone() is not None:
                continue  # déjà traité

            try:
                starter = thread.starter_message or await thread.fetch_message(thread.id)
                content = sanitize_text(f"{thread.name}\n\n{starter.content}")
            except Exception:
                content = sanitize_text(thread.name)

            if len(content.strip()) < 30:
                cur.execute("INSERT INTO processed_threads (thread_id) VALUES (%s) ON CONFLICT DO NOTHING", (thread_id,))
                conn.commit()
                continue

            new_questions = generate_questions_from_text(content, source_label=thread.name)
            total_saved += save_questions_to_db(new_questions, source=thread.name)
            threads_scanned += 1

            cur.execute("INSERT INTO processed_threads (thread_id) VALUES (%s) ON CONFLICT DO NOTHING", (thread_id,))
            conn.commit()
        cur.close()
    finally:
        release_conn(conn)

    return threads_scanned, total_saved

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
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS battle_cooldown_until TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS loser_until TEXT")
        # Rattrape best_streak pour les joueurs qui avaient déjà une streak en cours
        # avant l'ajout de cette colonne
        cur.execute("UPDATE users SET best_streak = streak WHERE best_streak = 0 AND streak > 0")
        # Récupère l'historique existant : comme aucun reset n'a encore eu lieu,
        # "points" représente déjà le total depuis le début pour l'instant.
        cur.execute("UPDATE users SET total_points = points WHERE total_points = 0 AND points > 0")

        # Banque de questions (remplace la liste codée en dur, alimentée aussi par l'IA)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id SERIAL PRIMARY KEY,
                q TEXT UNIQUE NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                points INTEGER NOT NULL,
                theme TEXT NOT NULL DEFAULT 'général',
                source TEXT
            )
        """)
        cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS theme TEXT NOT NULL DEFAULT 'général'")

        # Threads du forum déjà utilisés pour générer des questions (évite les doublons)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_threads (
                thread_id TEXT PRIMARY KEY
            )
        """)

        # Mémoire de conversation /chat, persistante entre les sessions
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_memory (
                user_id TEXT PRIMARY KEY,
                messages TEXT
            )
        """)

        # Mémoire partagée : ce que chaque joueur dit au bot, accessible à tous les autres joueurs
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shared_memory (
                id SERIAL PRIMARY KEY,
                author_name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Questions de départ (migrées une seule fois, ensuite tout vit en base)
        seed_questions = [
            ("Est ce une bonne idée de disso ?", "Oui", "ça dépend de qui est loup avec nous", "Non", "ça dépend de qui est dans la game", "D", "moyen", 2, "stratégie"),
            ("Quel est la meilleure catégorie de role pour une réflexion totale ?", "les roles a info", "les roles de protection", "les roles passifs", "les loups", "C", "difficile", 3, "rôles"),
            ("C'est quoi des gp complémentaires ?", "deux gp qui s'opposent mais ensemble avancent bien", "deux gp qui se ressemblent et avancent bien ensemble", "deux gp très différents qui se gênent l'un l'autre", "deux gp qui sont exactement les mêmes, sans impact sur l'autre", "A", "facile", 1, "vocabulaire"),
            ("Dans la technique du \"2 safes 1 loup\" au tour décisif (TD), combien de joueurs de chaque camp restent en jeu ?", "1 safe et 1 loup", "2 safes et 1 loup", "2 loups et 1 safe", "3 safes", "B", "facile", 1, "stratégie"),
            ("Pourquoi le maire doit-il laisser le vote temporairement en égalité pendant le tour décisif ?", "Pour perdre du temps sans raison", "Pour gagner du temps, se placer au centre de l'action et mettre le loup dans une situation critique", "Pour éviter d'avoir à voter", "Pour laisser le safe décider à sa place", "B", "moyen", 2, "stratégie"),
            ("Pourquoi le maire doit-il tuer immédiatement après avoir utilisé la phrase clé (\"oui merci, j'ai win\") ?", "Parce que reporter le choix laisse le loup se recalibrer, alors que tuer immédiatement exploite sa réaction spontanée", "Parce que c'est une règle du jeu obligatoire", "Parce que les autres joueurs préfèrent que ça aille vite", "Parce que le loup l'exige", "A", "difficile", 3, "stratégie"),
        ]
        for q, a, b, c, d, correct, difficulty, points, theme in seed_questions:
            cur.execute(
                "INSERT INTO questions (q, option_a, option_b, option_c, option_d, correct, difficulty, points, theme) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (q) DO NOTHING",
                (q, a, b, c, d, correct, difficulty, points, theme)
            )

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
# =========================================================
# QUIZ - les questions vivent maintenant en base (table "questions")
# =========================================================
def get_random_question(exclude_texts=None, theme=None, difficulty=None):
    """Pioche une question aléatoire dans la banque, en filtrant par thème et/ou difficulté si demandé
    (avec repli sur une question au hasard si aucune ne correspond)."""
    exclude_texts = exclude_texts or []
    columns = "q, option_a, option_b, option_c, option_d, correct, difficulty, points, theme"

    conn = get_conn()
    try:
        cur = conn.cursor()

        query = f"SELECT {columns} FROM questions WHERE 1=1"
        params = []
        if theme:
            query += " AND (theme ILIKE %s OR q ILIKE %s)"
            params.append(f"%{theme}%")
            params.append(f"%{theme}%")
        if difficulty:
            query += " AND difficulty = %s"
            params.append(difficulty)
        if exclude_texts:
            query += " AND q != ALL(%s)"
            params.append(exclude_texts)
        query += " ORDER BY RANDOM() LIMIT 1"

        cur.execute(query, tuple(params))
        row = cur.fetchone()
        matched_filter = row is not None

        if row is None:
            # Rien trouvé (filtre trop strict ou plus de questions inédites) -> repli sur du hasard total
            cur.execute(f"SELECT {columns} FROM questions ORDER BY RANDOM() LIMIT 1")
            row = cur.fetchone()

        cur.close()
    finally:
        release_conn(conn)

    if row is None:
        return None

    q, a, b, c, d, correct, difficulty_val, points, theme_val = row
    return {
        "q": q,
        "options": {"A": a, "B": b, "C": c, "D": d},
        "a": correct,
        "difficulty": difficulty_val,
        "points": points,
        "theme": theme_val,
        "matched_filter": matched_filter
    }

# =========================================================
# BOT DISCORD
# =========================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # nécessaire pour lister les membres (ping général, etc.)
bot = commands.Bot(command_prefix="!", intents=intents)

DAILY_LIMIT = 50

# Suivi des joueurs actuellement en plein duel (empêche les doublons de duel)
battle_active_users = set()

def has_loser_role(member):
    """Vérifie si un membre possède actuellement le rôle Loser, peu importe comment il l'a obtenu."""
    return discord.utils.get(member.roles, name="Loser") is not None

def get_battle_cooldown(user_id):
    """Retourne la date/heure jusqu'à laquelle le joueur ne peut pas relancer de duel (ou None)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT battle_cooldown_until FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
    finally:
        release_conn(conn)
    if row is None or row[0] is None:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None

def set_battle_penalty(user_id, until_dt):
    """Enregistre le cooldown et la fin du rôle Loser pour un joueur."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        exists = cur.fetchone()
        if exists:
            cur.execute(
                "UPDATE users SET battle_cooldown_until = %s, loser_until = %s WHERE user_id = %s",
                (until_dt.isoformat(), until_dt.isoformat(), user_id)
            )
        else:
            cur.execute(
                "INSERT INTO users (user_id, battle_cooldown_until, loser_until) VALUES (%s, %s, %s)",
                (user_id, until_dt.isoformat(), until_dt.isoformat())
            )
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)

async def get_or_create_loser_role(guild):
    role = discord.utils.get(guild.roles, name="Loser")
    if role is None:
        role = await guild.create_role(
            name="Loser",
            color=discord.Color.dark_gray(),
            reason="Rôle créé automatiquement pour /battle"
        )
    return role

@bot.event
async def on_member_update(before, after):
    """Dès que le rôle Loser est ajouté à quelqu'un (peu importe qui l'attribue), programme son retrait dans 1h."""
    before_role_ids = {r.id for r in before.roles}
    after_role_ids = {r.id for r in after.roles}
    newly_added = after_role_ids - before_role_ids

    if not newly_added:
        return

    loser_role = discord.utils.get(after.guild.roles, name="Loser")
    if loser_role and loser_role.id in newly_added:
        until = datetime.now() + timedelta(hours=1)
        set_battle_penalty(str(after.id), until)
        print(f"⏱️ Rôle Loser détecté sur {after.name} — retrait programmé dans 1h")

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
        super().__init__(timeout=45)
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

class BattleChallengeView(discord.ui.View):
    def __init__(self, challenger, opponent):
        super().__init__(timeout=30)
        self.challenger = challenger
        self.opponent = opponent
        self.result = None
        self.message = None

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("Ce duel ne te concerne pas !", ephemeral=True)
            return
        self.result = "accepted"
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"✅ {self.opponent.mention} a accepté le duel ! Début du combat...", view=self
        )
        self.stop()

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("Ce duel ne te concerne pas !", ephemeral=True)
            return
        self.result = "declined"
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"❌ {self.opponent.mention} a refusé le duel.", view=self
        )
        self.stop()

    async def on_timeout(self):
        if self.result is None and self.message is not None:
            for child in self.children:
                child.disabled = True
            try:
                await self.message.edit(content="⏰ Le duel a expiré, pas de réponse.", view=self)
            except Exception:
                pass

class BattleRoundView(discord.ui.View):
    def __init__(self, question, player1_id, player2_id):
        super().__init__(timeout=15)
        self.question = question
        self.player1_id = player1_id
        self.player2_id = player2_id
        self.winner_id = None
        self.finished = False
        self.answered_users = set()
        self.message = None

        for letter in ["A", "B", "C", "D"]:
            if letter in question["options"]:
                button = discord.ui.Button(label=letter, style=discord.ButtonStyle.primary)
                button.callback = self.make_callback(letter)
                self.add_item(button)

    def make_callback(self, letter):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id not in (self.player1_id, self.player2_id):
                await interaction.response.send_message("Ce duel ne te concerne pas !", ephemeral=True)
                return
            if self.finished:
                await interaction.response.defer()
                return
            if interaction.user.id in self.answered_users:
                await interaction.response.send_message("Tu as déjà répondu à cette question !", ephemeral=True)
                return

            self.answered_users.add(interaction.user.id)

            if letter == self.question["a"]:
                self.finished = True
                self.winner_id = interaction.user.id
                for child in self.children:
                    child.disabled = True
                embed = interaction.message.embeds[0]
                embed.description += f"\n\n✅ <@{interaction.user.id}> a trouvé la bonne réponse en premier !"
                embed.color = discord.Color.green()
                await interaction.response.edit_message(embed=embed, view=self)
                self.stop()
            else:
                await interaction.response.send_message("❌ Mauvaise réponse !", ephemeral=True)
                if len(self.answered_users) >= 2:
                    self.finished = True
                    for child in self.children:
                        child.disabled = True
                    embed = interaction.message.embeds[0]
                    embed.description += f"\n\n❌ Personne n'a trouvé la bonne réponse (**{self.question['a']}**)."
                    embed.color = discord.Color.orange()
                    try:
                        await interaction.message.edit(embed=embed, view=self)
                    except Exception:
                        pass
                    self.stop()
        return callback

    async def on_timeout(self):
        if self.finished or self.message is None:
            return
        self.finished = True
        for child in self.children:
            child.disabled = True
        try:
            embed = self.message.embeds[0]
            embed.description += "\n\n⏰ Personne n'a répondu à temps."
            embed.color = discord.Color.orange()
            await self.message.edit(embed=embed, view=self)
        except Exception:
            pass
        self.stop()

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

FORUM_CHANNEL_ID = os.getenv("FORUM_CHANNEL_ID")  # ID du salon forum "savoir", optionnel

@tasks.loop(hours=12)
async def auto_generate_questions():
    if not FORUM_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(int(FORUM_CHANNEL_ID)) or await bot.fetch_channel(int(FORUM_CHANNEL_ID))
        if isinstance(channel, discord.ForumChannel):
            scanned, saved = await generate_questions_from_forum(channel, limit=15)
            if saved > 0:
                print(f"🧠 Génération auto : {scanned} post(s) lus, {saved} nouvelle(s) question(s) ajoutée(s)")
    except Exception as e:
        print(f"Erreur génération auto: {e}")

@tasks.loop(minutes=5)
async def check_loser_roles():
    now = datetime.now()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, loser_until FROM users WHERE loser_until IS NOT NULL")
        rows = cur.fetchall()
        cur.close()
    finally:
        release_conn(conn)

    for user_id, loser_until_str in rows:
        try:
            loser_until = datetime.fromisoformat(loser_until_str)
        except Exception:
            continue

        if loser_until <= now:
            for guild in bot.guilds:
                try:
                    member = await guild.fetch_member(int(user_id))
                except Exception:
                    member = None
                if member:
                    role = discord.utils.get(guild.roles, name="Loser")
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="Fin de la sanction /battle")
                            print(f"✅ Rôle Loser retiré de {member.name}")
                        except Exception as e:
                            print(f"⚠️ Impossible de retirer le rôle Loser de {user_id}: {e}")

            conn2 = get_conn()
            try:
                cur2 = conn2.cursor()
                cur2.execute("UPDATE users SET loser_until = NULL WHERE user_id = %s", (user_id,))
                conn2.commit()
                cur2.close()
            finally:
                release_conn(conn2)

GUILD_ID = os.getenv("GUILD_ID")  # optionnel : ID de ton serveur, pour une sync instantanée des commandes

@bot.event
async def on_guild_join(guild):
    """Sécurité anti-vol : le bot quitte automatiquement tout serveur qui n'est pas le tien."""
    if GUILD_ID and str(guild.id) != GUILD_ID:
        print(f"⚠️ Tentative d'ajout sur un serveur non autorisé : {guild.name} ({guild.id}) — départ automatique.")
        await guild.leave()

@bot.event
async def on_ready():
    if GUILD_ID:
        guild_obj = discord.Object(id=int(GUILD_ID))
        # 1. Copie les commandes (encore en mémoire) vers le serveur, puis les synchronise
        bot.tree.copy_global_to(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)
        # 2. Nettoie ensuite les commandes globales pour éviter les doublons
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print(f"✅ Commandes synchronisées instantanément sur le serveur {GUILD_ID} (globales nettoyées)")
    else:
        await bot.tree.sync()
        print("ℹ️ Sync globale lancée (peut prendre jusqu'à 1h pour apparaître partout)")

    if not check_monthly_reset.is_running():
        check_monthly_reset.start()
    if not auto_generate_questions.is_running():
        auto_generate_questions.start()
    if not check_loser_roles.is_running():
        check_loser_roles.start()
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

async def search_and_generate_from_forum(theme):
    """Cherche un post du forum 'savoir' (FORUM_CHANNEL_ID) lié au thème demandé,
    génère une question à la volée à partir de son contenu, et la sauvegarde en base."""
    if not FORUM_CHANNEL_ID:
        return None

    try:
        channel = bot.get_channel(int(FORUM_CHANNEL_ID)) or await bot.fetch_channel(int(FORUM_CHANNEL_ID))
    except Exception:
        return None

    if not isinstance(channel, discord.ForumChannel):
        return None

    all_threads = list(channel.threads)
    try:
        async for archived in channel.archived_threads(limit=50):
            all_threads.append(archived)
    except Exception:
        pass

    theme_lower = theme.lower()

    # 1er passage : recherche dans les titres des posts
    matches = [t for t in all_threads if theme_lower in t.name.lower()]

    # 2e passage si rien trouvé : recherche dans le contenu des posts
    if not matches:
        for t in all_threads:
            try:
                starter = t.starter_message or await t.fetch_message(t.id)
                if theme_lower in starter.content.lower():
                    matches.append(t)
            except Exception:
                continue

    if not matches:
        return None

    import random as _random
    thread = _random.choice(matches)

    try:
        starter = thread.starter_message or await thread.fetch_message(thread.id)
        content = sanitize_text(f"{thread.name}\n\n{starter.content}")
    except Exception:
        content = sanitize_text(thread.name)

    new_questions = generate_questions_from_text(content, source_label=thread.name)
    if not new_questions:
        return None

    save_questions_to_db(new_questions, source=thread.name)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO processed_threads (thread_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (str(thread.id),)
        )
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)

    for q in new_questions:
        if theme_lower in q.get("theme", "").lower() or theme_lower in q["q"].lower():
            return q
    return new_questions[0]

@bot.tree.command(name="defi", description="Répond à une question quiz du serveur")
@app_commands.describe(
    theme="Thème souhaité pour la question (optionnel, ex: stratégie, rôles, vocabulaire)",
    difficulte="Niveau de difficulté souhaité (optionnel)"
)
@app_commands.choices(difficulte=[
    app_commands.Choice(name="Facile", value="facile"),
    app_commands.Choice(name="Moyen", value="moyen"),
    app_commands.Choice(name="Difficile", value="difficile"),
])
async def defi(interaction: discord.Interaction, theme: str = None, difficulte: app_commands.Choice[str] = None):
    if isinstance(interaction.user, discord.Member) and has_loser_role(interaction.user):
        await interaction.response.send_message(
            "🚫 Tu as le rôle **Loser**, tu ne peux pas jouer pour le moment !", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    difficulte_value = difficulte.value if difficulte else None
    question = get_random_question(theme=theme, difficulty=difficulte_value)

    live_generated = False
    if theme and (question is None or not question.get("matched_filter", True)):
        live_question = await search_and_generate_from_forum(theme)
        if live_question:
            question = live_question
            question["matched_filter"] = True
            live_generated = True

    if question is None:
        await interaction.followup.send("⚠️ Aucune question disponible pour l'instant.")
        return

    fallback_note = ""
    if live_generated:
        fallback_note = "\n\n*(Question générée à la volée depuis le forum \"savoir\" 🧠)*"
    elif (theme or difficulte_value) and not question.get("matched_filter", True):
        criteres = []
        if theme:
            criteres.append(f'thème "{theme}"')
        if difficulte_value:
            criteres.append(f'difficulté "{difficulte_value}"')
        fallback_note = f"\n\n*(Aucune question pour {' et '.join(criteres)}, en voici une au hasard.)*"

    options_text = "\n".join(
        f"**{letter})** {text}" for letter, text in question["options"].items()
    )

    embed = discord.Embed(
        title=f"🧠 Défi [{question['difficulty'].capitalize()} — {question['points']} pt(s)] — {question['theme'].capitalize()}",
        description=f"{question['q']}\n\n{options_text}{fallback_note}",
        color=discord.Color.blurple()
    )

    view = QuizView(question, interaction.user.id)
    view.message = await interaction.followup.send(embed=embed, view=view)

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
                "Tu es un pote sur un serveur Discord avec deux facettes bien distinctes. "
                "PAR DÉFAUT : tu es sympa, chaleureux, de bonne humeur, tu discutes normalement et gentiment. "
                "Tu peux taquiner légèrement de temps en temps, mais rien de méchant si on est correct avec toi. "
                "SEULEMENT si quelqu'un te manque de respect, t'insulte ou te clash en premier : là tu bascules "
                "et tu réponds du tac au tac avec une punchline sèche, cinglante et confiante, sans te démonter. "
                "Une fois la vanne passée, tu peux redevenir normal, pas besoin de rester agressif toute la conversation. "
                "Ton ton doit toujours rester naturel et fluide, jamais forcé ni caricatural (pas de jargon ado "
                "artificiel, pas d'expressions qui sonnent faux). Quand tu es mordant, évite de taper sur des trucs "
                "réellement sensibles comme la santé mentale, la solitude ou l'apparence physique — vise plutôt "
                "l'ironie et le retournement de situation. Pas de propos discriminants. "
                "Si on te demande de dire un truc trop frontal ou cru, ne t'excuse jamais et ne refuse jamais "
                "platement — trouve à la place une métaphore ou une image détournée qui fait passer le message "
                "avec du style. Exemple : au lieu de traiter quelqu'un de con directement, dis "
                "'vous êtes pas le lampadaire le plus lumineux du quartier'. "
                "Réponds de façon naturelle, concise (pas de pavé), et dans la même langue que l'utilisateur. "
                "Tu as accès à l'historique de vos échanges précédents dans cette conversation : appuie-toi "
                "dessus seulement si c'est vraiment pertinent pour la question posée. Ne dis jamais "
                "'on en a déjà parlé' ou une phrase similaire sauf si le sujet exact a vraiment été abordé "
                "avant dans l'historique fourni — sinon réponds directement à la question sans faire semblant "
                "de te souvenir de quelque chose qui n'a pas été dit."
            )
        }
    ]

    past_messages = load_chat_memory(str(user_id))
    if past_messages:
        active_chats[user_id].extend(past_messages)
        await interaction.response.send_message(
            "💬 Mode conversation activé ! Je me souviens de notre dernière discussion. "
            "Écris-moi ce que tu veux, je te réponds. Tape `/stopchat` pour arrêter."
        )
    else:
        await interaction.response.send_message(
            "💬 Mode conversation activé ! Écris-moi ce que tu veux, je te réponds. Tape `/stopchat` pour arrêter."
        )

@bot.tree.command(name="stopchat", description="Désactive le mode conversation avec l'IA")
async def stopchat(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in active_chats:
        del active_chats[user_id]
        await interaction.response.send_message("👋 Conversation terminée !")
    else:
        await interaction.response.send_message("Tu n'as pas de conversation active.", ephemeral=True)

PING_ALL_TRIGGERS = ["ping tout le monde", "ping tout le serveur", "ping everyone", "ping tlm"]
last_ping_all_time = None  # cooldown global anti-spam

def extract_ping_exceptions(message):
    """Retourne (ids explicitement mentionnés à exclure, noms tapés en texte à exclure)."""
    excluded_ids = {m.id for m in message.mentions}

    content_lower = message.content.lower()
    excluded_names = []
    if "sauf" in content_lower:
        after = content_lower.split("sauf", 1)[1]
        after_clean = re.sub(r"<@!?\d+>", "", after)  # retire les mentions déjà comptées
        parts = re.split(r"[,;/&]|(?:\bet\b)", after_clean)
        excluded_names = [p.strip() for p in parts if p.strip()]

    return excluded_ids, excluded_names

def is_ping_excluded(member, excluded_ids, excluded_names):
    if member.id in excluded_ids:
        return True
    display = member.display_name.lower()
    username = member.name.lower()
    for name in excluded_names:
        if name and (name in display or name in username or display in name or username in name):
            return True
    return False

async def handle_ping_all(message):
    global last_ping_all_time
    now = datetime.now()

    if last_ping_all_time and (now - last_ping_all_time) < timedelta(minutes=10):
        remaining = 10 - int((now - last_ping_all_time).total_seconds() / 60)
        await message.channel.send(f"⏳ Doucement, on a déjà ping tout le monde récemment. Réessaie dans ~{remaining} min.")
        return

    guild = message.guild
    try:
        members = [m async for m in guild.fetch_members(limit=None) if not m.bot]
    except Exception as e:
        await message.channel.send("⚠️ Impossible de récupérer la liste des membres.")
        print(f"Erreur fetch_members: {e}")
        return

    excluded_ids, excluded_names = extract_ping_exceptions(message)
    if excluded_ids or excluded_names:
        members = [m for m in members if not is_ping_excluded(m, excluded_ids, excluded_names)]

    if not members:
        await message.channel.send("Y'a personne à ping, le serveur est vide 👀")
        return

    last_ping_all_time = now

    try:
        intro = ask_groq([
            {"role": "system", "content": (
                "Tu es un troll potache qui va ping tout le serveur pour passer un message à tout le monde. "
                "Basé sur la demande de l'utilisateur, génère UNE SEULE phrase courte, drôle et un peu piquante "
                "ou cash (vanne, compliment, moquerie légère...) selon ce qui est demandé. "
                "Reste dans l'esprit troll bon enfant entre potes, mais évite les sujets réellement sensibles "
                "(santé mentale, physique, origine, etc.). "
                "Si la demande est trop frontale ou crue, ne t'excuse jamais et ne refuse jamais platement — "
                "trouve une métaphore ou une image détournée qui fait passer le message avec plus de style. "
                "Exemple : au lieu de traiter les gens de cons directement, dis "
                "'vous êtes pas les lampadaires les plus lumineux du quartier'."
            )},
            {"role": "user", "content": message.content}
        ])
    except Exception:
        intro = "🚨 Réveillez-vous tout le monde !"

    chunks = []
    current = intro.strip() + "\n\n"
    for member in members:
        piece = member.mention + " "
        if len(current) + len(piece) > 1900:
            chunks.append(current)
            current = ""
        current += piece
    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions(users=True))

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    user_id = message.author.id

    # Si l'utilisateur est en mode chat et que ce n'est pas une commande (!ping)
    if user_id in active_chats and not message.content.startswith("!"):
        lowered = message.content.lower()
        if message.guild and any(trigger in lowered for trigger in PING_ALL_TRIGGERS):
            await handle_ping_all(message)
            return

        active_chats[user_id].append({"role": "user", "content": message.content})
        log_shared_memory(message.author.display_name, message.content)

        async with message.channel.typing():
            try:
                shared_context = get_shared_memory_context(limit=20)
                messages_to_send = [active_chats[user_id][0]]  # le system prompt
                if shared_context:
                    messages_to_send.append({
                        "role": "system",
                        "content": (
                            "Voici des choses dites récemment par différents joueurs du serveur "
                            "(avec leur pseudo). Tu peux t'en servir et les répéter à d'autres joueurs "
                            "si c'est pertinent ou si on te le demande :\n" + shared_context
                        )
                    })
                messages_to_send.extend(active_chats[user_id][1:])

                reply = ask_groq(messages_to_send)
                active_chats[user_id].append({"role": "assistant", "content": reply})
                await message.channel.send(reply[:2000])  # limite Discord = 2000 caractères

                # Sauvegarde la mémoire (hors system prompt) pour la prochaine session
                non_system = [m for m in active_chats[user_id] if m["role"] != "system"]
                save_chat_memory(str(user_id), non_system)
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

@bot.tree.command(name="generer_questions", description="Génère des questions à partir des posts d'un forum (IA)")
@app_commands.describe(forum="Le salon forum à scanner (ex: savoir)", nombre="Nombre de posts max à analyser")
async def generer_questions(interaction: discord.Interaction, forum: discord.ForumChannel, nombre: int = 10):
    await interaction.response.defer()
    scanned, saved = await generate_questions_from_forum(forum, limit=nombre)
    await interaction.followup.send(
        f"🧠 Analyse terminée : {scanned} post(s) lu(s), **{saved} nouvelle(s) question(s)** ajoutée(s) à la banque."
    )

@bot.tree.command(name="battle", description="Défie un autre joueur en duel de quiz (5 questions, best of 5)")
@app_commands.describe(adversaire="Le joueur à défier")
async def battle(interaction: discord.Interaction, adversaire: discord.Member):
    challenger = interaction.user
    opponent = adversaire

    if opponent.id == challenger.id:
        await interaction.response.send_message("Tu ne peux pas te défier toi-même !", ephemeral=True)
        return
    if opponent.bot:
        await interaction.response.send_message("Tu ne peux pas défier un bot !", ephemeral=True)
        return

    for user in (challenger, opponent):
        if has_loser_role(user):
            await interaction.response.send_message(
                f"🚫 {user.mention} a le rôle **Loser**, impossible de jouer pour le moment.",
                ephemeral=True
            )
            return

    if challenger.id in battle_active_users or opponent.id in battle_active_users:
        await interaction.response.send_message("Un des deux joueurs est déjà en plein duel !", ephemeral=True)
        return

    battle_active_users.add(challenger.id)
    battle_active_users.add(opponent.id)

    try:
        challenge_view = BattleChallengeView(challenger, opponent)
        await interaction.response.send_message(
            f"⚔️ {challenger.mention} défie {opponent.mention} en duel ! "
            f"{opponent.mention}, accepte ou refuse ci-dessous (30s) :",
            view=challenge_view
        )
        challenge_view.message = await interaction.original_response()
        await challenge_view.wait()

        if challenge_view.result != "accepted":
            return

        scores = {challenger.id: 0, opponent.id: 0}
        used_questions = []

        for round_num in range(1, 6):
            question = get_random_question(exclude_texts=used_questions)
            if question is None:
                await interaction.followup.send("⚠️ Plus de questions disponibles pour continuer le duel.")
                break
            used_questions.append(question["q"])

            options_text = "\n".join(f"**{l})** {t}" for l, t in question["options"].items())
            embed = discord.Embed(
                title=f"⚔️ Round {round_num}/5 — [{question['difficulty'].capitalize()}]",
                description=(
                    f"{question['q']}\n\n{options_text}\n\n"
                    f"{challenger.mention} vs {opponent.mention} — le premier à trouver marque le point !"
                ),
                color=discord.Color.blurple()
            )
            round_view = BattleRoundView(question, challenger.id, opponent.id)
            round_message = await interaction.followup.send(embed=embed, view=round_view)
            round_view.message = round_message

            await round_view.wait()

            if round_view.winner_id:
                scores[round_view.winner_id] += 1

        p1_score = scores[challenger.id]
        p2_score = scores[opponent.id]

        if p1_score == p2_score:
            await interaction.followup.send(f"🤝 Égalité parfaite ({p1_score}-{p2_score}) ! Pas de perdant cette fois.")
        else:
            winner = challenger if p1_score > p2_score else opponent
            loser = opponent if winner.id == challenger.id else challenger

            await interaction.followup.send(
                f"🏆 {winner.mention} remporte le duel {max(p1_score, p2_score)}-{min(p1_score, p2_score)} !\n"
                f"💀 {loser.mention} écope du rôle **Loser** pendant 1h et ne pourra pas relancer de duel avant 1h."
            )

            try:
                loser_role = await get_or_create_loser_role(interaction.guild)
                await loser.add_roles(loser_role, reason="Perdant du duel /battle")
            except Exception as e:
                await interaction.followup.send(
                    f"⚠️ Impossible d'attribuer le rôle Loser (vérifie que le bot a la permission "
                    f"'Gérer les rôles' et qu'il est placé au-dessus du rôle Loser) : {e}"
                )

            until = datetime.now() + timedelta(hours=1)
            set_battle_penalty(str(loser.id), until)

    finally:
        battle_active_users.discard(challenger.id)
        battle_active_users.discard(opponent.id)

OWNER_ID = os.getenv("OWNER_ID")  # ton ID Discord, seul autorisé à utiliser /exclure_post

@bot.tree.command(name="exclure_post", description="Empêche un post du forum d'être utilisé pour générer des questions")
@app_commands.describe(post="Le post/thread du forum à exclure")
async def exclure_post(interaction: discord.Interaction, post: discord.Thread):
    if OWNER_ID and str(interaction.user.id) != OWNER_ID:
        await interaction.response.send_message("⛔ Seul le propriétaire du bot peut utiliser cette commande.", ephemeral=True)
        return
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO processed_threads (thread_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (str(post.id),)
        )
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)

    await interaction.response.send_message(
        f"🚫 Le post **{post.name}** est désormais exclu de la génération de questions (il ne sera jamais scanné).",
        ephemeral=True
    )

@bot.tree.command(name="reintegrer_post", description="Annule l'exclusion d'un post, il pourra à nouveau être scanné")
@app_commands.describe(post="Le post/thread du forum à réintégrer")
async def reintegrer_post(interaction: discord.Interaction, post: discord.Thread):
    if OWNER_ID and str(interaction.user.id) != OWNER_ID:
        await interaction.response.send_message("⛔ Seul le propriétaire du bot peut utiliser cette commande.", ephemeral=True)
        return

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM processed_threads WHERE thread_id = %s", (str(post.id),))
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)

    await interaction.response.send_message(
        f"✅ Le post **{post.name}** est réintégré, il pourra être scanné au prochain `/generer_questions`.",
        ephemeral=True
    )

@bot.tree.command(name="themes", description="Liste les thèmes de questions disponibles pour /defi")
async def themes(interaction: discord.Interaction):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT theme, COUNT(*) FROM questions GROUP BY theme ORDER BY COUNT(*) DESC")
        rows = cur.fetchall()
        cur.close()
    finally:
        release_conn(conn)

    if not rows:
        await interaction.response.send_message("Aucun thème disponible pour l'instant.", ephemeral=True)
        return

    lines = [f"• **{theme}** ({count} question{'s' if count > 1 else ''})" for theme, count in rows]
    await interaction.response.send_message(
        "📚 **Thèmes disponibles** (utilise `/defi theme:...`) :\n" + "\n".join(lines),
        ephemeral=True
    )

bot.run(os.getenv("TOKEN"))
