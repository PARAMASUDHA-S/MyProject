from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import os, re, logging, hashlib, sqlite3, csv, io, pymysql, pymysql.cursors, random, json
from groq import Groq
from datetime import datetime, timedelta
import uuid

GROQ_API_KEY = "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"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.template_folder = 'templates'
app.secret_key = os.environ.get('SECRET_KEY', 'ai-sql-assistant-secret-key-2025')
app.config['SESSION_PERMANENT'] = False

groq_client = Groq(api_key=GROQ_API_KEY)

# ─────────────────────────────────────────────
# DATABASE INIT
# ─────────────────────────────────────────────

def init_user_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS login_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT NOT NULL,
        login_at TEXT NOT NULL,
        ip_address TEXT,
        user_agent TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS otp_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        otp TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY,
        user_email TEXT NOT NULL,
        name TEXT NOT NULL,
        database_name TEXT,
        is_favorite INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        pending_sql TEXT,
        executed INTEGER DEFAULT 0,
        results TEXT,
        timestamp TEXT NOT NULL,
        FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS learning_progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT NOT NULL,
        topic TEXT NOT NULL,
        score INTEGER DEFAULT 0,
        completed INTEGER DEFAULT 0,
        last_attempt TEXT,
        UNIQUE(user_email, topic)
    )''')
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# MESSAGE PERSISTENCE
# ─────────────────────────────────────────────

def save_message(chat_id, role, content, pending_sql=None, timestamp=None):
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pending_sql_str = json.dumps(pending_sql) if pending_sql else None
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('INSERT INTO messages (chat_id, role, content, pending_sql, executed, results, timestamp) VALUES (?, ?, ?, ?, 0, NULL, ?)',
              (chat_id, role, content, pending_sql_str, timestamp))
    msg_id = c.lastrowid
    conn.commit(); conn.close()
    return msg_id


def mark_message_executed(chat_id, results):
    from decimal import Decimal
    def default_serializer(obj):
        if isinstance(obj, Decimal): return float(obj)
        raise TypeError(f'Object of type {obj.__class__.__name__} is not JSON serializable')
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''UPDATE messages SET executed = 1, results = ?, pending_sql = NULL
                 WHERE id = (SELECT id FROM messages WHERE chat_id = ? AND role = 'assistant'
                 AND pending_sql IS NOT NULL ORDER BY id DESC LIMIT 1)''',
              (json.dumps(results, default=default_serializer), chat_id))
    conn.commit(); conn.close()


def get_chat_history(chat_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT role, content, pending_sql, executed, results, timestamp FROM messages WHERE chat_id = ? ORDER BY id ASC', (chat_id,))
    rows = c.fetchall(); conn.close()
    history = []
    for role, content, pending_sql_str, executed, results_str, timestamp in rows:
        entry = {'role': role, 'content': content, 'timestamp': timestamp}
        if pending_sql_str: entry['pending_sql'] = json.loads(pending_sql_str)
        if executed: entry['executed'] = True
        if results_str: entry['results'] = json.loads(results_str)
        history.append(entry)
    return history


def clear_chat_history(chat_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
    conn.commit(); conn.close()


# ─────────────────────────────────────────────
# USER / AUTH HELPERS
# ─────────────────────────────────────────────

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def create_user(email, password):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)',
                  (email.lower().strip(), hash_password(password), datetime.now().isoformat()))
        conn.commit(); conn.close()
        return True, "Account created successfully"
    except sqlite3.IntegrityError:
        return False, "An account with this email already exists"
    except Exception as e:
        return False, str(e)


def verify_user(email, password):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT id, email FROM users WHERE email = ? AND password_hash = ?',
                  (email.lower().strip(), hash_password(password)))
        user = c.fetchone(); conn.close()
        return user
    except Exception:
        return None


def record_login(email, ip=None, ua=None):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('INSERT INTO login_history (user_email, login_at, ip_address, user_agent) VALUES (?, ?, ?, ?)',
              (email, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ip, ua))
    conn.commit(); conn.close()


def get_login_history(email, limit=20):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT login_at, ip_address, user_agent FROM login_history WHERE user_email = ? ORDER BY id DESC LIMIT ?',
              (email, limit))
    rows = c.fetchall(); conn.close()
    return [{'login_at': r[0], 'ip': r[1], 'ua': r[2]} for r in rows]


def generate_otp(email):
    otp = str(random.randint(100000, 999999))
    expires = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    # Invalidate old OTPs
    c.execute('UPDATE otp_tokens SET used = 1 WHERE email = ?', (email,))
    c.execute('INSERT INTO otp_tokens (email, otp, expires_at, used) VALUES (?, ?, ?, 0)', (email, otp, expires))
    conn.commit(); conn.close()
    return otp


def verify_otp(email, otp):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('SELECT id FROM otp_tokens WHERE email = ? AND otp = ? AND used = 0 AND expires_at > ?',
              (email, otp, now))
    row = c.fetchone()
    if row:
        c.execute('UPDATE otp_tokens SET used = 1 WHERE id = ?', (row[0],))
        conn.commit()
    conn.close()
    return row is not None


def user_exists(email):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT id FROM users WHERE email = ?', (email.lower().strip(),))
    row = c.fetchone(); conn.close()
    return row is not None


def reset_password(email, new_password):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE users SET password_hash = ? WHERE email = ?',
              (hash_password(new_password), email.lower().strip()))
    conn.commit(); conn.close()


# ─────────────────────────────────────────────
# CHAT DB HELPERS
# ─────────────────────────────────────────────

def get_user_chats(user_email):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT id, name, database_name, is_favorite, created_at, updated_at FROM chats WHERE user_email = ? ORDER BY is_favorite DESC, updated_at DESC', (user_email,))
    rows = c.fetchall(); conn.close()
    return [{'id': r[0], 'name': r[1], 'database_name': r[2], 'is_favorite': bool(r[3]), 'created_at': r[4], 'updated_at': r[5]} for r in rows]


def create_chat_db(user_email, name, database_name=None):
    chat_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('INSERT INTO chats (id, user_email, name, database_name, is_favorite, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)',
              (chat_id, user_email, name, database_name, now, now))
    conn.commit(); conn.close()
    return chat_id


def update_chat_db(chat_id, user_email, **kwargs):
    allowed = {'name', 'database_name', 'is_favorite'}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    fields['updated_at'] = datetime.now().isoformat()
    set_clause = ', '.join(f'{k} = ?' for k in fields)
    values = list(fields.values()) + [chat_id, user_email]
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute(f'UPDATE chats SET {set_clause} WHERE id = ? AND user_email = ?', values)
    conn.commit(); conn.close()


def delete_chat_db(chat_id, user_email):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('DELETE FROM chats WHERE id = ? AND user_email = ?', (chat_id, user_email))
    conn.commit(); conn.close()


def get_chat_db(chat_id, user_email):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT id, name, database_name, is_favorite FROM chats WHERE id = ? AND user_email = ?', (chat_id, user_email))
    row = c.fetchone(); conn.close()
    if row: return {'id': row[0], 'name': row[1], 'database_name': row[2], 'is_favorite': bool(row[3])}
    return None


init_user_db()

# ─────────────────────────────────────────────
# SESSION HELPERS
# ─────────────────────────────────────────────

def is_logged_in():
    return 'user_email' in session

def get_db_config():
    return session.get('db_config', None)


# ─────────────────────────────────────────────
# MYSQL HELPERS
# ─────────────────────────────────────────────

def try_mysql_connect(host, user, password, database=None):
    args = {'host': host, 'user': user, 'password': password, 'connect_timeout': 8, 'cursorclass': pymysql.cursors.Cursor}
    if database: args['database'] = database
    try:
        conn = pymysql.connect(**args)
        return conn, None
    except pymysql.err.OperationalError as e:
        code = e.args[0]
        msg = e.args[1] if len(e.args) > 1 else str(e)
        if code == 1045: return None, "Access denied — wrong username or password."
        elif code in (2003, 2002): return None, f"Cannot reach MySQL server at '{host}'."
        elif code == 1049: return None, f"Unknown database '{database}'."
        else: return None, f"Connection error ({code}): {msg}"
    except Exception as e:
        return None, str(e)


# ─────────────────────────────────────────────
# ROUTES — AUTH
# ─────────────────────────────────────────────

@app.route('/')
def index():
    if not is_logged_in(): return redirect(url_for('login'))
    if not get_db_config(): return redirect(url_for('db_connect'))
    chats = get_user_chats(session['user_email'])
    active_chat_id = session.get('active_chat_id')
    if active_chat_id and not any(c['id'] == active_chat_id for c in chats):
        active_chat_id = None; session.pop('active_chat_id', None)
    return render_template('chat.html', user_email=session['user_email'],
                           chats=chats, active_chat_id=active_chat_id, db_config=get_db_config())


@app.route('/login', methods=['GET', 'POST'])
def login():
    if is_logged_in(): return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        user = verify_user(email, password)
        if user:
            session.clear()
            session['user_email'] = user[1]
            record_login(user[1], ip=request.remote_addr, ua=request.headers.get('User-Agent', '')[:200])
            return redirect(url_for('index'))
        else:
            error = "Invalid email or password."
    return render_template('login.html', error=error)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if is_logged_in(): return redirect(url_for('index'))
    error = None; success = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if not email or not password: error = "Email and password are required."
        elif password != confirm: error = "Passwords do not match."
        elif len(password) < 6: error = "Password must be at least 6 characters."
        else:
            ok, msg = create_user(email, password)
            if ok: success = msg + " Please log in."
            else: error = msg
    return render_template('register.html', error=error, success=success)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ─────────────────────────────────────────────
# FORGOT PASSWORD (OTP via session simulation)
# ─────────────────────────────────────────────

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    return render_template('forgot_password.html')


@app.route('/api/send-otp', methods=['POST'])
def send_otp():
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip().lower()
    if not email: return jsonify({'ok': False, 'message': 'Email is required'})
    if not user_exists(email): return jsonify({'ok': False, 'message': 'No account found with this email.'})
    otp = generate_otp(email)
    # In production: send via email (SMTP). Here we log it and return for dev.
    logger.info(f"OTP for {email}: {otp}")
    # Store email in session for the reset step
    session['otp_email'] = email
    # Return OTP in response for demo (in prod, send via email only)
    return jsonify({'ok': True, 'message': f'OTP sent to {email}', 'dev_otp': otp})


@app.route('/api/verify-otp', methods=['POST'])
def verify_otp_route():
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip().lower()
    otp = data.get('otp', '').strip()
    if not email or not otp: return jsonify({'ok': False, 'message': 'Email and OTP required'})
    if verify_otp(email, otp):
        session['otp_verified_email'] = email
        return jsonify({'ok': True, 'message': 'OTP verified!'})
    return jsonify({'ok': False, 'message': 'Invalid or expired OTP.'})


@app.route('/api/reset-password', methods=['POST'])
def reset_password_route():
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    if not email or not password: return jsonify({'ok': False, 'message': 'Missing fields'})
    if session.get('otp_verified_email') != email:
        return jsonify({'ok': False, 'message': 'OTP not verified for this email'})
    if len(password) < 6: return jsonify({'ok': False, 'message': 'Password too short'})
    reset_password(email, password)
    session.pop('otp_verified_email', None)
    return jsonify({'ok': True, 'message': 'Password reset successfully!'})


# ─────────────────────────────────────────────
# ROUTES — DB CONNECT
# ─────────────────────────────────────────────

@app.route('/connect', methods=['GET', 'POST'])
def db_connect():
    if not is_logged_in(): return redirect(url_for('login'))
    error = None
    if request.method == 'POST':
        host = request.form.get('host', 'localhost').strip() or 'localhost'
        user = request.form.get('db_user', '').strip()
        password = request.form.get('db_password', '')
        conn, err = try_mysql_connect(host, user, password)
        if conn:
            conn.close()
            session['db_config'] = {'host': host, 'user': user, 'password': password}
            return redirect(url_for('index'))
        else: error = err
    return render_template('connect.html', error=error)


@app.route('/test_connection', methods=['POST'])
def test_connection():
    if not is_logged_in(): return jsonify({'ok': False, 'message': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    host = data.get('host', 'localhost').strip() or 'localhost'
    user = data.get('user', '').strip()
    password = data.get('password', '')
    conn, err = try_mysql_connect(host, user, password)
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT VERSION()")
            version = cursor.fetchone()[0]
            cursor.execute("SHOW DATABASES")
            dbs = [r[0] for r in cursor.fetchall()]
            cursor.close(); conn.close()
            return jsonify({'ok': True, 'version': version, 'databases': dbs})
        except Exception as e:
            conn.close()
            return jsonify({'ok': True, 'version': 'unknown', 'databases': []})
    else: return jsonify({'ok': False, 'message': err})


@app.route('/disconnect', methods=['POST'])
def db_disconnect():
    session.pop('db_config', None); session.pop('active_chat_id', None)
    return redirect(url_for('db_connect'))


# ─────────────────────────────────────────────
# ROUTES — CHAT
# ─────────────────────────────────────────────

@app.route('/chat/new', methods=['POST'])
def new_chat():
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    name = data.get('name', '').strip()
    database_name = data.get('database_name', '').strip() or None
    if not name: return jsonify({'error': 'Chat name is required'}), 400
    chat_id = create_chat_db(session['user_email'], name, database_name)
    session['active_chat_id'] = chat_id; session.modified = True
    return jsonify({'id': chat_id, 'name': name, 'database_name': database_name, 'is_favorite': False})


@app.route('/chat/<chat_id>/select', methods=['POST'])
def select_chat(chat_id):
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    chat = get_chat_db(chat_id, session['user_email'])
    if not chat: return jsonify({'error': 'Chat not found'}), 404
    session['active_chat_id'] = chat_id; session.modified = True
    history = get_chat_history(chat_id)
    return jsonify({'chat': chat, 'history': history})


@app.route('/chat/<chat_id>/rename', methods=['POST'])
def rename_chat(chat_id):
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    name = data.get('name', '').strip()
    if not name: return jsonify({'error': 'Name required'}), 400
    update_chat_db(chat_id, session['user_email'], name=name)
    return jsonify({'ok': True})


@app.route('/chat/<chat_id>/favorite', methods=['POST'])
def toggle_favorite(chat_id):
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    chat = get_chat_db(chat_id, session['user_email'])
    if not chat: return jsonify({'error': 'Not found'}), 404
    new_fav = not chat['is_favorite']
    update_chat_db(chat_id, session['user_email'], is_favorite=int(new_fav))
    return jsonify({'is_favorite': new_fav})


@app.route('/chat/<chat_id>/delete', methods=['POST'])
def delete_chat_route(chat_id):
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    delete_chat_db(chat_id, session['user_email'])
    if session.get('active_chat_id') == chat_id:
        session.pop('active_chat_id', None)
    session.modified = True
    return jsonify({'ok': True})


@app.route('/list_databases', methods=['GET'])
def list_databases():
    if not is_logged_in() or not get_db_config(): return jsonify({'error': 'Not authenticated'}), 401
    db_cfg = get_db_config()
    conn, err = try_mysql_connect(db_cfg['host'], db_cfg['user'], db_cfg['password'])
    if not conn: return jsonify({"status": "error", "message": err})
    try:
        cursor = conn.cursor()
        cursor.execute("SHOW DATABASES")
        databases = [db[0] for db in cursor.fetchall()]
        cursor.close(); conn.close()
        return jsonify({"status": "success", "databases": databases})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/process', methods=['POST'])
def process_data():
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    if not get_db_config(): return jsonify({'error': 'No database connected'}), 400
    data = request.get_json(silent=True)
    if not data: return jsonify({'error': 'Invalid JSON'}), 400
    user_message = data.get('message', '').strip()
    chat_id = data.get('chat_id', session.get('active_chat_id'))
    if not user_message: return jsonify({'error': 'Empty message'}), 400
    if not chat_id: return jsonify({'error': 'No active chat'}), 400
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_message(chat_id, 'user', user_message, timestamp=timestamp)
    chat = get_chat_db(chat_id, session['user_email'])
    chat_db = chat['database_name'] if chat else None
    full_history = get_chat_history(chat_id)
    recent_history = full_history[-17:-1]
    ai_response = get_ai_response(user_message, recent_history, chat_db)
    sql_commands = re.findall(r'<SQL>(.*?)</SQL>', ai_response, re.DOTALL)
    sql_list = [s.strip() for s in sql_commands]
    save_message(chat_id, 'assistant', ai_response,
                 pending_sql=sql_list if sql_list else None,
                 timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    update_chat_db(chat_id, session['user_email'])
    return jsonify({'response': ai_response, 'sql_commands': sql_list, 'needs_confirmation': len(sql_list) > 0})


@app.route('/execute', methods=['POST'])
def execute_confirmed():
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    if not get_db_config(): return jsonify({'error': 'No database connected'}), 400
    data = request.get_json(silent=True)
    sql_commands = data.get('sql_commands', [])
    chat_id = data.get('chat_id', session.get('active_chat_id'))
    if not sql_commands: return jsonify({'error': 'No SQL commands provided'}), 400
    chat_db = None
    if chat_id:
        chat = get_chat_db(chat_id, session['user_email'])
        chat_db = chat['database_name'] if chat else None
    results = [{'sql': sql.strip(), 'result': execute_sql(sql.strip(), chat_db)} for sql in sql_commands]
    if chat_id: mark_message_executed(chat_id, results)
    return jsonify({'results': results, 'status': 'success'})


@app.route('/clear_history', methods=['POST'])
def clear_history():
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    chat_id = data.get('chat_id', session.get('active_chat_id'))
    if chat_id: clear_chat_history(chat_id)
    return jsonify({"status": "success"})


# ─────────────────────────────────────────────
# LEARNING PLATFORM ROUTES
# ─────────────────────────────────────────────

@app.route('/learn')
def learn():
    if not is_logged_in(): return redirect(url_for('login'))
    return render_template('learn.html', user_email=session['user_email'])


@app.route('/api/learn/progress', methods=['GET'])
def get_progress():
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT topic, score, completed, last_attempt FROM learning_progress WHERE user_email = ?',
              (session['user_email'],))
    rows = c.fetchall(); conn.close()
    return jsonify({r[0]: {'score': r[1], 'completed': bool(r[2]), 'last_attempt': r[3]} for r in rows})


@app.route('/api/learn/save-progress', methods=['POST'])
def save_progress():
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    topic = data.get('topic', '').strip()
    score = data.get('score', 0)
    completed = data.get('completed', False)
    if not topic: return jsonify({'error': 'Topic required'}), 400
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''INSERT INTO learning_progress (user_email, topic, score, completed, last_attempt)
                 VALUES (?, ?, ?, ?, ?)
                 ON CONFLICT(user_email, topic) DO UPDATE SET
                   score = MAX(score, excluded.score),
                   completed = excluded.completed,
                   last_attempt = excluded.last_attempt''',
              (session['user_email'], topic, score, int(completed), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/learn/ask', methods=['POST'])
def learn_ask():
    """AI tutor endpoint for learning platform."""
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    question = data.get('question', '').strip()
    topic = data.get('topic', 'SQL Basics')
    if not question: return jsonify({'error': 'Question required'}), 400
    system_prompt = f"""You are a friendly and encouraging SQL tutor helping a student learn {topic}.

Your teaching style:
- Explain concepts clearly with simple analogies
- Always include working SQL examples wrapped in code blocks
- Be encouraging and patient
- When showing SQL, use proper formatting
- Keep answers focused and not too long
- If the student is wrong, correct them kindly and explain why
- Use emojis sparingly to keep the tone friendly

Current topic: {topic}
"""
    try:
        completion = groq_client.chat.completions.create(
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": question}],
            model="llama-3.1-8b-instant", temperature=0.4, max_tokens=800
        )
        return jsonify({'response': completion.choices[0].message.content})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/learn/check-answer', methods=['POST'])
def check_answer():
    """Check a user's SQL answer for an exercise."""
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    question = data.get('question', '')
    user_answer = data.get('answer', '').strip()
    expected = data.get('expected', '')
    if not user_answer: return jsonify({'correct': False, 'feedback': 'Please write an answer first.'})
    system_prompt = """You are an SQL exercise checker. Evaluate if the student's SQL answer is correct.
Respond with JSON only: {"correct": true/false, "feedback": "brief explanation", "correct_answer": "the proper SQL if wrong"}
Be lenient about whitespace, capitalization, and alias names. Focus on logical correctness."""
    try:
        completion = groq_client.chat.completions.create(
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": f"Question: {question}\nExpected: {expected}\nStudent answer: {user_answer}"}],
            model="llama-3.1-8b-instant", temperature=0.1, max_tokens=300
        )
        raw = completion.choices[0].message.content
        clean = re.sub(r'```json|```', '', raw).strip()
        result = json.loads(clean)
        return jsonify(result)
    except Exception as e:
        return jsonify({'correct': False, 'feedback': f'Could not evaluate: {str(e)}'})


@app.route('/api/learn/generate-quiz', methods=['POST'])
def generate_quiz():
    """Generate random SQL quiz questions for a topic."""
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    topic = data.get('topic', 'SQL Basics')
    difficulty = data.get('difficulty', 'beginner')
    system_prompt = f"""Generate 5 multiple-choice SQL quiz questions about "{topic}" at {difficulty} level.
Return ONLY valid JSON in this exact format:
[
  {{
    "question": "What does SELECT do?",
    "options": ["A. Deletes rows", "B. Retrieves data", "C. Updates rows", "D. Creates tables"],
    "correct": 1,
    "explanation": "SELECT is used to retrieve data from a database."
  }}
]
Make questions practical and educational. correct is the 0-based index of the correct option."""
    try:
        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": system_prompt}],
            model="llama-3.1-8b-instant", temperature=0.6, max_tokens=1200
        )
        raw = completion.choices[0].message.content
        clean = re.sub(r'```json|```', '', raw).strip()
        # Extract JSON array
        match = re.search(r'\[.*\]', clean, re.DOTALL)
        if match:
            questions = json.loads(match.group())
            return jsonify({'questions': questions})
        return jsonify({'error': 'Could not parse questions'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/login-history', methods=['GET'])
def login_history_route():
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    return jsonify(get_login_history(session['user_email']))


# ─────────────────────────────────────────────
# AI + SQL HELPERS
# ─────────────────────────────────────────────

def get_database_schema(database=None):
    db_cfg = get_db_config()
    if not db_cfg: return "No database connected"
    try:
        conn, err = try_mysql_connect(db_cfg['host'], db_cfg['user'], db_cfg['password'], database)
        if not conn: return f"Cannot read schema: {err}"
        cursor = conn.cursor()
        if database:
            cursor.execute("SHOW TABLES")
            tables = [t[0] for t in cursor.fetchall()]
            schema_info = []
            for table in tables:
                cursor.execute(f"DESCRIBE `{table}`")
                columns = cursor.fetchall()
                col_info = [f"{c[0]} ({c[1]})" for c in columns]
                schema_info.append(f"Table '{table}': {', '.join(col_info)}")
            cursor.close(); conn.close()
            return "\n".join(schema_info) if schema_info else "No tables found"
        else:
            cursor.execute("SHOW DATABASES")
            dbs = [d[0] for d in cursor.fetchall()]
            cursor.close(); conn.close()
            return f"Available databases: {', '.join(dbs)}"
    except Exception as e:
        return f"Unable to retrieve schema: {str(e)}"


def get_ai_response(user_message, history, chat_db=None):
    table_info = get_database_schema(chat_db)
    db_cfg = get_db_config()
    db_context = f"Connected to: host={db_cfg['host']}, user={db_cfg['user']}, database={chat_db or '(none selected)'}"
    system_prompt = f"""You are an AI Based SQL Query Assistant that helps users run database operations.

DATABASE CONNECTION: {db_context}
DATABASE CONTEXT: {table_info}

YOUR JOB:
- For database operations: explain in plain English, show SQL in <SQL>...</SQL> tags, end with "Press **Alt+Enter** or click **Execute** to run."
- For general questions: respond conversationally WITHOUT SQL tags.

RULES:
- Always wrap SQL inside <SQL>your SQL here</SQL> tags.
- Never execute anything yourself.
- Use only table/column names from the schema above.
"""
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        if 'content' in msg and msg.get('role') in ('user', 'assistant'):
            messages.append({"role": msg['role'], "content": msg['content']})
    messages.append({"role": "user", "content": user_message})
    try:
        cc = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", temperature=0.3, top_p=0.9)
        return cc.choices[0].message.content
    except Exception as e:
        return f"Error connecting to AI service: {str(e)}"


def execute_sql(sql, database=None):
    db_cfg = get_db_config()
    if not db_cfg: return {"type": "ERROR", "error": "No database connected"}
    try:
        conn, err = try_mysql_connect(db_cfg['host'], db_cfg['user'], db_cfg['password'], database)
        if not conn: return {"type": "ERROR", "error": err}
        cursor = conn.cursor()
        cursor.execute(sql)
        sql_upper = sql.strip().upper()
        if sql_upper.startswith('SELECT') or sql_upper.startswith('SHOW') or sql_upper.startswith('DESCRIBE'):
            columns = [col[0] for col in cursor.description]
            rows = [list(r) for r in cursor.fetchall()]
            result = {"type": "SELECT", "columns": columns, "rows": rows, "count": len(rows)}
        else:
            conn.commit()
            result = {"type": sql.strip().split()[0].upper(), "rows_affected": cursor.rowcount, "status": "success"}
        cursor.close(); conn.close()
        return result
    except Exception as e:
        return {"type": "ERROR", "error": str(e), "sql": sql}


# ─────────────────────────────────────────────
# CSV HELPERS
# ─────────────────────────────────────────────

def sanitize_column_name(name):
    name = name.strip()
    name = re.sub(r'[^\w]', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    if not name: name = 'col'
    if name[0].isdigit(): name = 'c_' + name
    return name.lower()


def sanitize_table_name(name):
    name = re.sub(r'[^\w]', '_', name.strip())
    name = re.sub(r'_+', '_', name).strip('_').lower()
    if not name: name = 'imported_table'
    if name[0].isdigit(): name = 't_' + name
    return name


def infer_mysql_type(values):
    non_empty = [v.strip() for v in values if v.strip()]
    if not non_empty: return 'VARCHAR(255)'
    try:
        [int(v) for v in non_empty]
        max_val = max(abs(int(v)) for v in non_empty)
        return 'BIGINT' if max_val > 2147483647 else 'INT'
    except ValueError: pass
    try:
        [float(v) for v in non_empty]; return 'DOUBLE'
    except ValueError: pass
    if all(re.match(r'^\d{4}-\d{2}-\d{2}$', v) for v in non_empty): return 'DATE'
    if all(re.match(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}', v) for v in non_empty): return 'DATETIME'
    max_len = max(len(v) for v in non_empty)
    if max_len <= 50: return 'VARCHAR(100)'
    if max_len <= 255: return 'VARCHAR(255)'
    return 'TEXT'


@app.route('/csv_preview', methods=['POST'])
def csv_preview():
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    if 'file' not in request.files: return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.csv'): return jsonify({'error': 'Only CSV files are supported'}), 400
    raw = f.read()
    try: text = raw.decode('utf-8-sig')
    except: text = raw.decode('latin-1')
    lines = text.splitlines()
    if len(lines) < 2: return jsonify({'error': 'CSV must have header + data rows'}), 400
    sample = '\n'.join(lines[:5])
    dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
    reader = csv.reader(io.StringIO(text), dialect)
    rows_raw = [r for r in reader if any(c.strip() for c in r)]
    if len(rows_raw) < 2: return jsonify({'error': 'No data rows found'}), 400
    headers_raw = rows_raw[0]; data_rows = rows_raw[1:]
    seen = {}; columns = []
    for h in headers_raw:
        safe = sanitize_column_name(h) or 'col'
        if safe in seen:
            seen[safe] += 1; safe = f'{safe}_{seen[safe]}'
        else: seen[safe] = 0
        columns.append({'original': h, 'safe': safe})
    sample_rows = data_rows[:200]
    col_types = [infer_mysql_type([r[i] if i < len(r) else '' for r in sample_rows]) for i, _ in enumerate(columns)]
    suggested_table = sanitize_table_name(os.path.splitext(f.filename)[0])
    all_rows_clean = [[c.strip() for c in (r[:len(columns)] + [''] * max(0, len(columns) - len(r)))] for r in data_rows]
    preview_rows = all_rows_clean[:10]
    null_counts = [0] * len(columns)
    for row in all_rows_clean:
        for i, v in enumerate(row):
            if not v: null_counts[i] += 1
    seen_keys = set(); duplicate_count = 0
    for row in all_rows_clean:
        key = tuple(v.lower() for v in row)
        if key in seen_keys: duplicate_count += 1
        else: seen_keys.add(key)
    return jsonify({'columns': [c['safe'] for c in columns], 'original_headers': [c['original'] for c in columns],
                    'col_types': col_types, 'preview_rows': preview_rows, 'total_rows': len(data_rows),
                    'suggested_table': suggested_table, 'all_rows': all_rows_clean,
                    'null_counts': null_counts, 'duplicate_count': duplicate_count})


@app.route('/csv_preprocess', methods=['POST'])
def csv_preprocess():
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    data = request.get_json(silent=True)
    if not data: return jsonify({'error': 'Invalid request'}), 400
    all_rows = data.get('all_rows', []); columns = data.get('columns', [])
    operations = data.get('operations', {}); original_count = len(all_rows); report = []
    if operations.get('remove_duplicates'):
        before = len(all_rows); seen = set(); deduped = []
        for row in all_rows:
            key = tuple(v.strip().lower() for v in row)
            if key not in seen: seen.add(key); deduped.append(row)
        all_rows = deduped; report.append(f"Removed {before - len(all_rows)} duplicate row(s)")
    if operations.get('remove_blank_rows'):
        before = len(all_rows)
        all_rows = [r for r in all_rows if any(v.strip() for v in r)]
        report.append(f"Removed {before - len(all_rows)} fully blank row(s)")
    null_strategy = operations.get('null_strategy', 'keep')
    if null_strategy == 'remove_rows':
        before = len(all_rows)
        all_rows = [r for r in all_rows if all(v.strip() for v in r)]
        report.append(f"Removed {before - len(all_rows)} row(s) with any null/empty cell")
    elif null_strategy == 'fill_empty':
        fill_val = operations.get('fill_value', 'N/A'); count = 0; cleaned = []
        for row in all_rows:
            new_row = []
            for v in row:
                if not v.strip(): new_row.append(fill_val); count += 1
                else: new_row.append(v)
            cleaned.append(new_row)
        all_rows = cleaned; report.append(f"Filled {count} empty cell(s) with '{fill_val}'")
    elif null_strategy == 'fill_zero':
        count = 0; cleaned = []
        for row in all_rows:
            new_row = []
            for v in row:
                if not v.strip(): new_row.append('0'); count += 1
                else: new_row.append(v)
            cleaned.append(new_row)
        all_rows = cleaned; report.append(f"Filled {count} empty cell(s) with '0'")
    if operations.get('trim_whitespace', True):
        all_rows = [[v.strip() for v in row] for row in all_rows]
        report.append("Trimmed whitespace from all values")
    case_map = operations.get('column_case', {})
    if case_map:
        for row in all_rows:
            for idx_str, mode in case_map.items():
                idx = int(idx_str)
                if idx < len(row):
                    v = row[idx]
                    if mode == 'upper': row[idx] = v.upper()
                    elif mode == 'lower': row[idx] = v.lower()
                    elif mode == 'title': row[idx] = v.title()
        report.append(f"Applied case standardization to {len(case_map)} column(s)")
    required_cols = operations.get('required_columns', [])
    if required_cols:
        before = len(all_rows)
        all_rows = [r for r in all_rows if all((r[i].strip() if i < len(r) else '') for i in required_cols)]
        report.append(f"Removed {before - len(all_rows)} row(s) missing values in required columns")
    null_counts = [0] * len(columns)
    for row in all_rows:
        for i, v in enumerate(row[:len(columns)]):
            if not v.strip(): null_counts[i] += 1
    return jsonify({'all_rows': all_rows, 'preview_rows': all_rows[:10], 'total_rows': len(all_rows),
                    'original_count': original_count, 'removed_count': original_count - len(all_rows),
                    'null_counts': null_counts, 'report': report})


@app.route('/csv_import', methods=['POST'])
def csv_import():
    if not is_logged_in(): return jsonify({'error': 'Not logged in'}), 401
    db_cfg = get_db_config()
    if not db_cfg: return jsonify({'error': 'No database connected'}), 400
    data = request.get_json(silent=True)
    if not data: return jsonify({'error': 'Invalid request'}), 400
    table_name = sanitize_table_name(data.get('table_name', ''))
    columns = data.get('columns', []); col_types = data.get('col_types', [])
    all_rows = data.get('all_rows', []); if_exists = data.get('if_exists', 'error')
    add_id = data.get('add_id', True); database = data.get('database', None)
    if not table_name or not columns or not all_rows: return jsonify({'error': 'Missing required fields'}), 400
    conn, err = try_mysql_connect(db_cfg['host'], db_cfg['user'], db_cfg['password'], database)
    if not conn: return jsonify({'error': err}), 500
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = %s", (database, table_name))
        exists = cursor.fetchone()[0] > 0
        if exists:
            if if_exists == 'error': return jsonify({'error': f"Table `{table_name}` already exists."}), 409
            elif if_exists == 'replace':
                cursor.execute(f"DROP TABLE `{table_name}`"); conn.commit(); exists = False
        if not exists:
            col_defs = ['`id` INT AUTO_INCREMENT PRIMARY KEY'] if add_id else []
            col_defs += [f'`{col}` {typ}' for col, typ in zip(columns, col_types)]
            cursor.execute(f"CREATE TABLE `{table_name}` ({', '.join(col_defs)})"); conn.commit()
        placeholders = ', '.join(['%s'] * len(columns))
        col_names = ', '.join(f'`{c}`' for c in columns)
        insert_sql = f"INSERT INTO `{table_name}` ({col_names}) VALUES ({placeholders})"
        inserted = errors = 0
        for i in range(0, len(all_rows), 500):
            batch = all_rows[i:i + 500]; batch_values = []
            for row in batch:
                vals = []
                for v, typ in zip(row, col_types):
                    v = v.strip() if isinstance(v, str) else v
                    if v == '' or v is None: vals.append(None)
                    elif 'INT' in typ:
                        try: vals.append(int(v))
                        except: vals.append(None)
                    elif typ in ('DOUBLE', 'FLOAT'):
                        try: vals.append(float(v))
                        except: vals.append(None)
                    else: vals.append(v)
                batch_values.append(tuple(vals))
            try:
                cursor.executemany(insert_sql, batch_values); conn.commit(); inserted += len(batch)
            except: errors += len(batch)
        cursor.close(); conn.close()
        return jsonify({'status': 'success', 'table': table_name, 'inserted': inserted, 'errors': errors,
                        'message': f"✅ Table `{table_name}` created with {inserted} rows inserted." + (f" ({errors} rows skipped)" if errors else "")})
    except Exception as e:
        conn.rollback(); cursor.close(); conn.close()
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True)
