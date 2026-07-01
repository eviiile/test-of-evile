import os
import logging
import requests
import time
import json
from datetime import datetime, timedelta
from functools import wraps
from contextlib import contextmanager
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response

import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'evile-secret-key-2026')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'evile2026')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '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')
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DATABASE_URL = "postgresql://evile_site_user:yxWlZVZsC39DhRtXoY7e84ci6NTJgcaR@dpg-d8mpl3rsq97s739pscq0-a.oregon-postgres.render.com/evile_site"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_characters_cache = {'data': None, 'timestamp': 0}
CACHE_TTL = 300

@contextmanager
def get_db():
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        yield cur
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Database error: {str(e)}")
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def ensure_notification_columns(cur):
    cur.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name='notifications' AND column_name='duration_hours'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE notifications ADD COLUMN duration_hours INTEGER DEFAULT 1")
        logger.info("Added column duration_hours to notifications table")
    
    cur.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name='notifications' AND column_name='show_in_chat'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE notifications ADD COLUMN show_in_chat BOOLEAN DEFAULT FALSE")
        logger.info("Added column show_in_chat to notifications table")

def ensure_character_columns(cur):
    cur.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name='characters' AND column_name='is_max'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE characters ADD COLUMN is_max BOOLEAN DEFAULT FALSE")
        logger.info("✅ Added missing column 'is_max' to characters table")

# --- إضافة جدول طلبات MAX ---
def ensure_max_requests_table(cur):
    cur.execute('''CREATE TABLE IF NOT EXISTS max_requests (
        id SERIAL PRIMARY KEY,
        telegram_id TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    logger.info("✅ Ensured max_requests table exists")

def init_db():
    try:
        with get_db() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS characters (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                prompt TEXT NOT NULL,
                callback_key TEXT UNIQUE NOT NULL,
                logo_url TEXT DEFAULT ''
            )''')
            cur.execute('''CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            cur.execute('''CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id TEXT UNIQUE NOT NULL,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            ensure_notification_columns(cur)
            ensure_character_columns(cur)
            ensure_max_requests_table(cur) # إضافة الجدول الجديد
            
            logger.info("Database initialized/updated successfully")
            print("✅ Database tables ensured successfully.")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        print(f"❌ Critical DB Init Error: {e}")
        raise

def update_user_activity(telegram_id):
    if not telegram_id: return
    try:
        with get_db() as cur:
            cur.execute("UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE telegram_id = %s", (telegram_id,))
    except Exception as e:
        logger.error(f"Update activity error: {e}")

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('admin_panel'))
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def index():
    telegram_id = session.get('telegram_id')
    if telegram_id: update_user_activity(telegram_id)
    try:
        with get_db() as cur:
            cur.execute('SELECT * FROM characters ORDER BY id')
            characters = cur.fetchall()
            cur.execute('SELECT * FROM notifications WHERE show_in_chat = true ORDER BY created_at DESC LIMIT 1')
            latest_notification = cur.fetchone()
    except Exception as e:
        logger.error(f"Index error: {e}")
        characters = []
        latest_notification = None
    return render_template('index.html',
                         characters=characters,
                         telegram_id=telegram_id,
                         latest_notification=latest_notification)

@app.route('/register', methods=['POST'])
def register():
    try:
        telegram_id = request.form.get('telegram_id', '').strip()
        if not telegram_id or not telegram_id.isdigit():
            return jsonify({'success': False, 'message': 'معرّف غير صحيح'}), 400
        with get_db() as cur:
            cur.execute(
                "INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT (telegram_id) DO UPDATE SET last_active = CURRENT_TIMESTAMP",
                (telegram_id,)
            )
        session['telegram_id'] = telegram_id
        session.permanent = True
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Register error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/active_users')
def api_active_users():
    try:
        with get_db() as cur:
            cur.execute("SELECT COUNT(*) FROM users WHERE last_active > NOW() - INTERVAL '5 minutes'")
            row = cur.fetchone()
            count = row['count'] if row else 0
        return jsonify({'count': count})
    except Exception as e:
        logger.error(f"Active users error: {e}")
        return jsonify({'count': 0})

@app.route('/health')
def health_check():
    try:
        with get_db() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
            db_ok = row is not None
        return jsonify({
            'status': 'healthy' if db_ok else 'unhealthy',
            'database': 'connected' if db_ok else 'disconnected',
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500

@app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('admin_panel'))
        flash('كلمة المرور غير صحيحة', 'error')
    
    if session.get('logged_in'):
        try:
            with get_db() as cur:
                cur.execute('SELECT * FROM characters ORDER BY id DESC')
                characters = cur.fetchall()
                cur.execute('SELECT * FROM notifications ORDER BY id DESC')
                notifications = cur.fetchall()
                cur.execute('SELECT COUNT(*) FROM users')
                row = cur.fetchone()
                users_count = row['count'] if row else 0
        except Exception as e:
            logger.error(f"Admin panel data error: {e}")
            characters, notifications, users_count = [], [], 0
        return render_template('admin.html', characters=characters, notifications=notifications, users_count=users_count)
    
    return render_template('admin.html')

@app.route('/admin/logout')
def logout():
    session.clear()
    return redirect(url_for('admin_panel'))

@app.route('/admin/character/add', methods=['POST'])
@admin_required
def add_character():
    name = request.form.get('name')
    description = request.form.get('description')
    prompt = request.form.get('prompt')
    callback_key = request.form.get('callback_key', name.lower().replace(' ', '_'))
    logo_url = request.form.get('logo_url', '')
    is_max = request.form.get('is_max') == 'on'
    if name and description and prompt:
        try:
            with get_db() as cur:
                cur.execute("INSERT INTO characters (name, description, prompt, callback_key, logo_url, is_max) VALUES (%s, %s, %s, %s, %s, %s)",
                    (name, description, prompt, callback_key, logo_url, is_max))
            flash('تمت إضافة الشخصية بنجاح', 'success')
        except Exception as e:
            flash('مفتاح الشخصية موجود مسبقاً' if 'unique' in str(e).lower() else str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/character/<int:char_id>/edit', methods=['POST'])
@admin_required
def edit_character(char_id):
    name = request.form.get('name')
    description = request.form.get('description')
    prompt = request.form.get('prompt')
    logo_url = request.form.get('logo_url', '')
    is_max = request.form.get('is_max') == 'on'
    if name and description and prompt:
        try:
            with get_db() as cur:
                cur.execute("UPDATE characters SET name=%s, description=%s, prompt=%s, logo_url=%s, is_max=%s WHERE id=%s",
                    (name, description, prompt, logo_url, is_max, char_id))
            flash('تم تعديل الشخصية بنجاح', 'success')
        except Exception as e:
            flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/character/<int:char_id>/delete')
@admin_required
def delete_character(char_id):
    try:
        with get_db() as cur:
            cur.execute("DELETE FROM characters WHERE id=%s", (char_id,))
        flash('تم حذف الشخصية', 'success')
    except Exception as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/notification/add', methods=['POST'])
@admin_required
def add_notification():
    title = request.form.get('title')
    text = request.form.get('text')
    duration_hours = request.form.get('duration_hours', 1, type=int)
    show_in_chat = request.form.get('show_in_chat') == 'on'
    if title and text:
        try:
            with get_db() as cur:
                cur.execute(
                    "INSERT INTO notifications (title, text, duration_hours, show_in_chat) VALUES (%s, %s, %s, %s)",
                    (title, text, duration_hours, show_in_chat)
                )
            flash('تم إرسال الإشعار بنجاح', 'success')
        except Exception as e:
            flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/notification/<int:notif_id>/delete')
@admin_required
def delete_notification(notif_id):
    try:
        with get_db() as cur:
            cur.execute("DELETE FROM notifications WHERE id=%s", (notif_id,))
        flash('تم حذف الإشعار', 'success')
    except Exception as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/api/characters')
def api_characters():
    now = time.time()
    if _characters_cache['data'] and (now - _characters_cache['timestamp']) < CACHE_TTL:
        return jsonify(_characters_cache['data'])
    try:
        with get_db() as cur:
            cur.execute('SELECT * FROM characters ORDER BY id')
            data = cur.fetchall()
        _characters_cache['data'] = data
        _characters_cache['timestamp'] = now
        return jsonify(data)
    except Exception as e:
        logger.error(f"API characters error: {e}")
        return jsonify([])

@app.route('/api/notifications')
def api_notifications():
    try:
        with get_db() as cur:
            cur.execute('SELECT * FROM notifications ORDER BY id DESC')
            return jsonify(cur.fetchall())
    except Exception as e:
        logger.error(f"API notifications error: {e}")
        return jsonify([])

# --- API جديد لطلبات MAX ---
@app.route('/api/request_max', methods=['POST'])
def request_max():
    data = request.json
    telegram_id = data.get('telegram_id', '').strip()
    if not telegram_id or not telegram_id.isdigit():
        return jsonify({'success': False, 'message': 'معرف غير صحيح'}), 400
    try:
        with get_db() as cur:
            # التحقق مما إذا كان قد أرسل طلباً معلقاً مسبقاً
            cur.execute("SELECT * FROM max_requests WHERE telegram_id=%s AND status='pending'", (telegram_id,))
            if cur.fetchone():
                return jsonify({'success': False, 'message': 'لديك طلب معلق مسبقاً، يرجى انتظار الرد.'}), 400
            cur.execute("INSERT INTO max_requests (telegram_id) VALUES (%s)", (telegram_id,))
        return jsonify({'success': True, 'message': 'تم استلام طلبك بنجاح، سيتم التواصل معك.'})
    except Exception as e:
        logger.error(f"Max request error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/max_requests', methods=['GET'])
@admin_required
def get_max_requests():
    try:
        with get_db() as cur:
            cur.execute("SELECT * FROM max_requests ORDER BY created_at DESC")
            requests = cur.fetchall()
        return jsonify(requests)
    except Exception as e:
        logger.error(f"Admin max requests error: {e}")
        return jsonify([])

@app.route('/api/admin/max_requests/<int:req_id>/<action>', methods=['POST'])
@admin_required
def handle_max_request(req_id, action):
    if action not in ['approve', 'reject']:
        return jsonify({'success': False, 'message': 'إجراء غير صحيح'}), 400
    try:
        with get_db() as cur:
            status = 'approved' if action == 'approve' else 'rejected'
            cur.execute("UPDATE max_requests SET status=%s WHERE id=%s", (status, req_id))
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Handle max request error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.json
    character_key = data.get('character', 'logo_maker')
    message = data.get('message', '')
    try:
        with get_db() as cur:
            cur.execute("SELECT * FROM characters WHERE callback_key=%s", (character_key,))
            character = cur.fetchone()
    except Exception as e:
        logger.error(f"Get character error: {e}")
        return jsonify({'error': 'Database connection error: ' + str(e)}), 500
    if not character:
        return jsonify({'error': 'Character not found'}), 404
    
    headers = {
        'Authorization': f'Bearer {OPENROUTER_API_KEY}',
        'Content-Type': 'application/json',
        'HTTP-Referer': request.url_root,
        'X-Title': 'EVILE'
    }
    payload = {
        'model': 'openrouter/auto',
        'messages': [
            {'role': 'system', 'content': character['prompt']},
            {'role': 'user', 'content': message}
        ],
        'temperature': 0.7,
        'stream': True
    }
    
    def generate():
        try:
            response = requests.post(OPENROUTER_URL, json=payload, headers=headers, stream=True, timeout=30)
            response.raise_for_status()
            for chunk in response.iter_lines():
                if chunk:
                    decoded = chunk.decode('utf-8')
                    if decoded.startswith('data: '):
                        data_str = decoded[6:]
                        if data_str != '[DONE]':
                            try:
                                json_data = json.loads(data_str)
                                content = json_data['choices'][0]['delta'].get('content', '')
                                if content:
                                    yield content
                            except Exception as e:
                                logger.error(f"JSON decode error in stream: {e}")
        except requests.exceptions.RequestException as e:
            logger.error(f"OpenRouter API Request Exception: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Status Code: {e.response.status_code}")
                logger.error(f"Response Body: {e.response.text}")
            yield f"خطأ في الاتصال بـ OpenRouter (تفاصيل الخطأ موجودة في السجلات)"
        except Exception as e:
            logger.error(f"Unexpected error in OpenRouter stream: {e}")
            yield f"حدث خطأ غير متوقع: {str(e)}"
    
    return Response(generate(), mimetype='text/plain')

print("🚀 Starting EVILE Application...")
try:
    init_db()
except Exception as e:
    print(f"❌ FATAL ERROR: Database initialization failed! {e}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False)
