import os
import logging
import requests
import time as time_module
import json
from datetime import datetime, timedelta, time
from functools import wraps
from contextlib import contextmanager
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'evile-secret-key-2026')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'evile2026')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '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')
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DATABASE_URL = "postgresql://evile_site_user:yxWlZVZsC39DhRtXoY7e84ci6NTJgcaR@dpg-d8mpl3rsq97s739pscq0-a.oregon-postgres.render.com/evile_site"
BOT_TOKEN = os.getenv('BOT_TOKEN', '')

TIMEZONE = pytz.timezone('Asia/Aden')

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

_characters_cache = {'data': None, 'timestamp': 0}
CACHE_TTL = 300

# ==================== قاعدة البيانات ====================
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

def ensure_published_posts_columns(cur):
    cur.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name='published_posts' AND column_name='content_id'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE published_posts ADD COLUMN content_id INTEGER DEFAULT NULL")
        logger.info("Added column content_id to published_posts table")

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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                duration_hours INTEGER DEFAULT 1,
                show_in_chat BOOLEAN DEFAULT FALSE
            )''')
            cur.execute('''CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id TEXT UNIQUE NOT NULL,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS publish_channels (
                id SERIAL PRIMARY KEY,
                telegram_id TEXT NOT NULL UNIQUE,
                channel_id TEXT NOT NULL UNIQUE,
                channel_username TEXT,
                channel_name TEXT,
                channel_bio TEXT,
                members_count INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                is_paused BOOLEAN DEFAULT FALSE,
                selected_content_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_post_at TIMESTAMP
            )''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS publish_contents (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                prompt TEXT NOT NULL,
                publish_time TIME NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS published_posts (
                id SERIAL PRIMARY KEY,
                channel_id TEXT NOT NULL,
                content TEXT NOT NULL,
                content_id INTEGER,
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                message_id TEXT
            )''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS scheduled_content (
                id SERIAL PRIMARY KEY,
                content_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                scheduled_time TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            # ====== جدول الإعلانات ======
            cur.execute('''CREATE TABLE IF NOT EXISTS ads (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                link TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            ensure_notification_columns(cur)
            ensure_published_posts_columns(cur)
            logger.info("Database initialized/updated successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        raise

# ==================== دوال مساعدة ====================
def update_user_activity(telegram_id):
    if not telegram_id:
        return
    try:
        with get_db() as cur:
            cur.execute("UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE telegram_id = %s", (telegram_id,))
    except Exception as e:
        logger.error(f"Update activity error: {e}")

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def get_telegram_channel_info(channel_username):
    if not BOT_TOKEN:
        return None
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChat?chat_id={channel_username}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get('ok'):
            chat = data['result']
            return {
                'name': chat.get('title', ''),
                'bio': chat.get('description', ''),
                'username': chat.get('username', ''),
                'members_count': 0
            }
        return None
    except Exception as e:
        logger.error(f"Error getting channel info: {e}")
        return None

def get_channel_members_count(channel_username):
    if not BOT_TOKEN:
        return 0
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMembersCount?chat_id={channel_username}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get('ok'):
            return data['result']
        return 0
    except Exception as e:
        logger.error(f"Error getting members count: {e}")
        return 0

def send_telegram_message(channel_id, text, parse_mode='Markdown'):
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set")
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': channel_id,
        'text': text,
        'parse_mode': parse_mode,
        'disable_web_page_preview': False
    }
    try:
        response = requests.post(url, json=payload, timeout=30)
        data = response.json()
        if data.get('ok'):
            return data['result']['message_id']
        else:
            logger.error(f"Telegram API error: {data}")
            return None
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return None

def generate_post_content(content_id=None, custom_prompt=None):
    """توليد محتوى باستخدام البرومبت من admin فقط - لا يوجد برومبت افتراضي"""
    prompt = None
    
    # 1. جلب البرومبت باستخدام content_id إذا وجد
    if content_id:
        with get_db() as cur:
            cur.execute("SELECT prompt FROM publish_contents WHERE id = %s", (content_id,))
            row = cur.fetchone()
            if row and row['prompt']:
                prompt = row['prompt']
                logger.info(f"Using prompt from content_id {content_id}")
    
    # 2. إذا لم يكن هناك برومبت من content_id، نبحث عن أول محتوى في قاعدة البيانات
    if not prompt:
        with get_db() as cur:
            cur.execute("SELECT id, prompt FROM publish_contents ORDER BY id LIMIT 1")
            row = cur.fetchone()
            if row and row['prompt']:
                prompt = row['prompt']
                logger.info(f"Using prompt from first content (id: {row['id']})")
    
    # 3. استخدام البرومبت المخصص إذا وُجد
    if not prompt and custom_prompt:
        prompt = custom_prompt
        logger.info("Using custom prompt from request")
    
    # 4. إذا لم يوجد أي برومبت في قاعدة البيانات، نعيد None
    if not prompt:
        logger.error("No content found in database. Please add content from admin panel.")
        return None
    
    headers = {
        'Authorization': f'Bearer {OPENROUTER_API_KEY}',
        'Content-Type': 'application/json',
        'HTTP-Referer': 'https://evile.onrender.com',
        'X-Title': 'EVILE Publisher'
    }
    
    payload = {
        'model': 'openai/gpt-4o-mini',
        'messages': [
            {'role': 'system', 'content': 'أنت كاتب محتوى تقني محترف. اكتب مقالة قصيرة وجذابة.'},
            {'role': 'user', 'content': prompt}
        ],
        'temperature': 0.9,
        'max_tokens': 400,
        'top_p': 0.95
    }
    
    try:
        response = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=60)
        result = response.json()
        if result and 'choices' in result and len(result['choices']) > 0:
            message = result['choices'][0].get('message', {})
            content = message.get('content')
            if content:
                return content.strip()
        return None
    except Exception as e:
        logger.error(f"Error generating content: {e}")
        return None

def format_ad_message(ad):
    """تنسيق الإعلان إلى نص قابل للنشر"""
    message = f"📢 *{ad['title']}*\n\n{ad['content']}"
    if ad.get('link'):
        message += f"\n\n🔗 [رابط إضافي]({ad['link']})"
    return message

# ==================== جدولة النشر ====================
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()

def schedule_posts():
    try:
        with get_db() as cur:
            cur.execute("SELECT * FROM publish_channels WHERE is_active = true AND is_paused = false")
            channels = cur.fetchall()
            cur.execute("SELECT * FROM publish_contents")
            contents = cur.fetchall()
            
            for channel in channels:
                for job in scheduler.get_jobs():
                    if job.id.startswith(f'publish_{channel["id"]}_'):
                        scheduler.remove_job(job.id)
                
                for content in contents:
                    content_id = content['id']
                    publish_time = content['publish_time']
                    hour = publish_time.hour
                    minute = publish_time.minute
                    
                    job_id = f'publish_{channel["id"]}_{content_id}'
                    trigger = CronTrigger(hour=hour, minute=minute, timezone=TIMEZONE)
                    scheduler.add_job(
                        func=publish_content_to_channel,
                        trigger=trigger,
                        id=job_id,
                        args=[channel['channel_id'], content_id],
                        replace_existing=True
                    )
                    logger.info(f"Scheduled content {content_id} for channel {channel['channel_id']} at {hour}:{minute}")
    except Exception as e:
        logger.error(f"Error scheduling posts: {e}")

def publish_content_to_channel(channel_id, content_id):
    try:
        with get_db() as cur:
            cur.execute("SELECT * FROM publish_channels WHERE channel_id = %s AND is_active = true AND is_paused = false", (channel_id,))
            channel = cur.fetchone()
            if not channel:
                return
            
            # 1. نشر المحتوى
            content_text = generate_post_content(content_id=content_id)
            if content_text:
                message_id = send_telegram_message(channel_id, content_text)
                if message_id:
                    cur.execute(
                        "INSERT INTO published_posts (channel_id, content, content_id, message_id) VALUES (%s, %s, %s, %s)",
                        (channel_id, content_text, content_id, message_id)
                    )
                    cur.execute("UPDATE publish_channels SET last_post_at = NOW() WHERE channel_id = %s", (channel_id,))
                    logger.info(f"Published content {content_id} to channel {channel_id}")
                else:
                    logger.error(f"Failed to send content to channel {channel_id}")
            else:
                logger.error(f"Failed to generate content for channel {channel_id}")
            
            # 2. نشر الإعلانات النشطة
            cur.execute("SELECT * FROM ads WHERE is_active = true ORDER BY id")
            ads = cur.fetchall()
            for ad in ads:
                ad_message = format_ad_message(ad)
                message_id = send_telegram_message(channel_id, ad_message)
                if message_id:
                    logger.info(f"Published ad {ad['id']} to channel {channel_id}")
                else:
                    logger.error(f"Failed to send ad {ad['id']} to channel {channel_id}")
                    
    except Exception as e:
        logger.error(f"Error publishing content: {e}")

# ==================== Routes ====================
@app.route('/')
def index():
    telegram_id = session.get('telegram_id')
    characters = []
    latest_notification = None
    try:
        with get_db() as cur:
            cur.execute('SELECT * FROM characters ORDER BY id')
            characters = cur.fetchall() or []
            cur.execute('SELECT * FROM notifications WHERE show_in_chat = true ORDER BY created_at DESC LIMIT 1')
            latest_notification = cur.fetchone()
    except Exception as e:
        logger.error(f"Index error: {e}")
        characters = []
        latest_notification = None
    channel_url = "https://t.me/Evile_Prompts"
    instagram_url = "https://www.instagram.com/bla6c7"
    return render_template('index.html',
                         characters=characters,
                         telegram_id=telegram_id,
                         latest_notification=latest_notification,
                         channel_url=channel_url,
                         instagram_url=instagram_url)

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
        logger.info(f"User {telegram_id} registered")
        return jsonify({'success': True, 'redirect': '/publish'})
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

# ==================== Routes النشر ====================
@app.route('/publish/state')
def publish_state():
    telegram_id = session.get('telegram_id')
    response = {
        'telegram_id': telegram_id,
        'has_channel': False,
        'has_agreed': session.get('publish_agreed', False),
        'needs_login': False
    }
    
    if telegram_id:
        with get_db() as cur:
            cur.execute("SELECT * FROM publish_channels WHERE telegram_id = %s", (telegram_id,))
            channel = cur.fetchone()
            if channel:
                response['has_channel'] = True
                channel_dict = dict(channel)
                for key, value in channel_dict.items():
                    if isinstance(value, datetime):
                        channel_dict[key] = value.isoformat()
                    elif isinstance(value, time):
                        channel_dict[key] = value.isoformat()
                response['channel'] = channel_dict
                
                cur.execute("""
                    SELECT pc.*, 
                           (SELECT COUNT(*) FROM publish_channels WHERE selected_content_id = pc.id) as channels_count
                    FROM publish_contents pc
                    ORDER BY pc.id
                """)
                contents = cur.fetchall()
                contents_list = []
                for c in contents:
                    c_dict = dict(c)
                    if isinstance(c_dict.get('publish_time'), (datetime, time)):
                        c_dict['publish_time'] = c_dict['publish_time'].isoformat()
                    contents_list.append(c_dict)
                response['contents'] = contents_list
                
                cur.execute("SELECT * FROM published_posts WHERE channel_id = %s ORDER BY published_at DESC LIMIT 5", (channel['channel_id'],))
                recent_posts = cur.fetchall()
                posts_list = []
                for p in recent_posts:
                    p_dict = dict(p)
                    if isinstance(p_dict.get('published_at'), datetime):
                        p_dict['published_at'] = p_dict['published_at'].isoformat()
                    posts_list.append(p_dict)
                response['recent_posts'] = posts_list
            else:
                response['has_channel'] = False
    else:
        response['needs_login'] = True
    
    return jsonify(response)

@app.route('/publish/agree', methods=['POST'])
def publish_agree():
    telegram_id = session.get('telegram_id')
    if not telegram_id:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    session['publish_agreed'] = True
    return jsonify({'success': True})

@app.route('/publish/register', methods=['POST'])
def publish_register_channel():
    telegram_id = session.get('telegram_id')
    if not telegram_id:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    channel_username = request.form.get('channel_username', '').strip()
    if not channel_username.startswith('@'):
        return jsonify({'success': False, 'message': 'اسم المستخدم يجب أن يبدأ بـ @'}), 400
    
    channel_info = get_telegram_channel_info(channel_username)
    if not channel_info:
        return jsonify({'success': False, 'message': 'لم يتم العثور على القناة. تأكد من إضافة البوت كمشرف.'}), 400
    
    members_count = get_channel_members_count(channel_username)
    
    try:
        with get_db() as cur:
            cur.execute("SELECT id FROM publish_channels WHERE telegram_id = %s", (telegram_id,))
            if cur.fetchone():
                return jsonify({'success': False, 'message': 'لديك قناة مسجلة مسبقاً'}), 400
            
            cur.execute("""
                INSERT INTO publish_channels 
                (telegram_id, channel_id, channel_username, channel_name, channel_bio, members_count)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (telegram_id, channel_username, channel_username, channel_info['name'], channel_info['bio'], members_count))
        
        schedule_posts()
        return jsonify({'success': True, 'message': 'تم تسجيل القناة بنجاح'})
    except Exception as e:
        logger.error(f"Error registering channel: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/publish/select_content', methods=['POST'])
def publish_select_content():
    telegram_id = session.get('telegram_id')
    if not telegram_id:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    content_id = request.form.get('content_id')
    if not content_id:
        return jsonify({'success': False, 'message': 'لم يتم اختيار محتوى'}), 400
    
    try:
        with get_db() as cur:
            cur.execute("UPDATE publish_channels SET selected_content_id = %s WHERE telegram_id = %s", (content_id, telegram_id))
        return jsonify({'success': True, 'message': 'تم اختيار المحتوى بنجاح'})
    except Exception as e:
        logger.error(f"Error selecting content: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/publish/toggle_pause')
def publish_toggle_pause():
    telegram_id = session.get('telegram_id')
    if not telegram_id:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    try:
        with get_db() as cur:
            cur.execute("SELECT is_paused, id FROM publish_channels WHERE telegram_id = %s", (telegram_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'message': 'لا توجد قناة مسجلة'}), 400
            
            new_status = not row['is_paused']
            cur.execute("UPDATE publish_channels SET is_paused = %s WHERE telegram_id = %s", (new_status, telegram_id))
            
            if not new_status:
                schedule_posts()
            else:
                for job in scheduler.get_jobs():
                    if job.id.startswith(f'publish_{row["id"]}_'):
                        scheduler.remove_job(job.id)
            
        return jsonify({'success': True, 'is_paused': new_status})
    except Exception as e:
        logger.error(f"Error toggling pause: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/publish/stop')
def publish_stop():
    telegram_id = session.get('telegram_id')
    if not telegram_id:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    try:
        with get_db() as cur:
            cur.execute("SELECT id FROM publish_channels WHERE telegram_id = %s", (telegram_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'message': 'لا توجد قناة مسجلة'}), 400
            
            for job in scheduler.get_jobs():
                if job.id.startswith(f'publish_{row["id"]}_'):
                    scheduler.remove_job(job.id)
            
            cur.execute("UPDATE publish_channels SET is_active = false WHERE id = %s", (row['id'],))
        
        return jsonify({'success': True, 'message': 'تم إيقاف النشر نهائياً'})
    except Exception as e:
        logger.error(f"Error stopping publishing: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/publish/force_publish')
def publish_force():
    telegram_id = session.get('telegram_id')
    if not telegram_id:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    try:
        with get_db() as cur:
            cur.execute("SELECT channel_id, selected_content_id FROM publish_channels WHERE telegram_id = %s AND is_active = true", (telegram_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'message': 'لا توجد قناة نشطة'}), 400
            
            content_id = row['selected_content_id']
            if not content_id:
                cur.execute("SELECT id FROM publish_contents ORDER BY id LIMIT 1")
                content_row = cur.fetchone()
                if content_row:
                    content_id = content_row['id']
                else:
                    return jsonify({'success': False, 'message': 'لا يوجد محتوى متاح. أضف محتوى من لوحة الإدارة.'}), 400
            
            content = generate_post_content(content_id=content_id)
            if not content:
                return jsonify({'success': False, 'message': 'فشل توليد المحتوى. تأكد من وجود برومبت في المحتوى المختار.'}), 500
            
            # نشر المحتوى
            message_id = send_telegram_message(row['channel_id'], content)
            if message_id:
                cur.execute(
                    "INSERT INTO published_posts (channel_id, content, content_id, message_id) VALUES (%s, %s, %s, %s)",
                    (row['channel_id'], content, content_id, message_id)
                )
                cur.execute("UPDATE publish_channels SET last_post_at = NOW() WHERE channel_id = %s", (row['channel_id'],))
                
                # نشر الإعلانات النشطة
                cur.execute("SELECT * FROM ads WHERE is_active = true ORDER BY id")
                ads = cur.fetchall()
                for ad in ads:
                    ad_message = format_ad_message(ad)
                    send_telegram_message(row['channel_id'], ad_message)
                
                return jsonify({'success': True, 'message': 'تم النشر بنجاح'})
            else:
                return jsonify({'success': False, 'message': 'فشل النشر'}), 500
    except Exception as e:
        logger.error(f"Error forcing publish: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ==================== Admin Routes ====================
@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('admin_panel'))
        flash('كلمة المرور غير صحيحة', 'error')
    return render_template('login.html')

@app.route('/admin/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
def admin_panel():
    logged_in = session.get('logged_in', False)
    if not logged_in:
        return render_template('admin.html', logged_in=False)
    
    try:
        with get_db() as cur:
            cur.execute('SELECT * FROM characters ORDER BY id DESC')
            characters = cur.fetchall()
            cur.execute('SELECT * FROM notifications ORDER BY id DESC')
            notifications = cur.fetchall()
            cur.execute('SELECT COUNT(*) FROM users')
            row = cur.fetchone()
            users_count = row['count'] if row else 0
            
            cur.execute('SELECT * FROM publish_contents ORDER BY id DESC')
            contents = cur.fetchall()
            
            cur.execute('SELECT * FROM publish_channels ORDER BY created_at DESC')
            channels = cur.fetchall()
            
            cur.execute('SELECT * FROM ads ORDER BY id DESC')
            ads = cur.fetchall()
    except Exception as e:
        logger.error(f"Admin panel error: {e}")
        characters, notifications, users_count, contents, channels, ads = [], [], 0, [], [], []
    
    return render_template('admin.html',
                         logged_in=True,
                         characters=characters,
                         notifications=notifications,
                         users_count=users_count,
                         contents=contents,
                         channels=channels,
                         ads=ads)

# ==================== Admin: Characters ====================
@app.route('/admin/character/add', methods=['POST'])
@admin_required
def add_character():
    name = request.form.get('name')
    description = request.form.get('description')
    prompt = request.form.get('prompt')
    callback_key = request.form.get('callback_key', name.lower().replace(' ', '_'))
    logo_url = request.form.get('logo_url', '')
    if name and description and prompt:
        try:
            with get_db() as cur:
                cur.execute("INSERT INTO characters (name, description, prompt, callback_key, logo_url) VALUES (%s, %s, %s, %s, %s)",
                    (name, description, prompt, callback_key, logo_url))
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
    if name and description and prompt:
        try:
            with get_db() as cur:
                cur.execute("UPDATE characters SET name=%s, description=%s, prompt=%s, logo_url=%s WHERE id=%s",
                    (name, description, prompt, logo_url, char_id))
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

# ==================== Admin: Notifications ====================
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

# ==================== Admin: Content Management ====================
@app.route('/admin/content/add', methods=['POST'])
@admin_required
def admin_add_content():
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    prompt = request.form.get('prompt', '').strip()
    publish_time = request.form.get('publish_time', '').strip()
    
    if not name or not prompt or not publish_time:
        flash('جميع الحقول مطلوبة', 'error')
        return redirect(url_for('admin_panel'))
    
    try:
        time_obj = datetime.strptime(publish_time, '%H:%M').time()
        with get_db() as cur:
            cur.execute(
                "INSERT INTO publish_contents (name, description, prompt, publish_time) VALUES (%s, %s, %s, %s)",
                (name, description, prompt, time_obj)
            )
        flash('تم إضافة المحتوى بنجاح', 'success')
        schedule_posts()
    except Exception as e:
        logger.error(f"Error adding content: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/content/<int:content_id>/edit', methods=['POST'])
@admin_required
def admin_edit_content(content_id):
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    prompt = request.form.get('prompt', '').strip()
    publish_time = request.form.get('publish_time', '').strip()
    
    if not name or not prompt or not publish_time:
        flash('جميع الحقول مطلوبة', 'error')
        return redirect(url_for('admin_panel'))
    
    try:
        time_obj = datetime.strptime(publish_time, '%H:%M').time()
        with get_db() as cur:
            cur.execute(
                "UPDATE publish_contents SET name = %s, description = %s, prompt = %s, publish_time = %s WHERE id = %s",
                (name, description, prompt, time_obj, content_id)
            )
        flash('تم تعديل المحتوى بنجاح', 'success')
        schedule_posts()
    except Exception as e:
        logger.error(f"Error editing content: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/content/<int:content_id>/delete')
@admin_required
def admin_delete_content(content_id):
    try:
        with get_db() as cur:
            # إلغاء المهام المجدولة لهذا المحتوى
            for job in scheduler.get_jobs():
                if job.id.endswith(f'_{content_id}'):
                    scheduler.remove_job(job.id)
            # حذف من جدول scheduled_content إذا وجد
            try:
                cur.execute("DELETE FROM scheduled_content WHERE content_id = %s", (content_id,))
            except Exception as e:
                logger.warning(f"Could not delete from scheduled_content: {e}")
            # حذف من جدول publish_contents
            cur.execute("DELETE FROM publish_contents WHERE id = %s", (content_id,))
        flash('تم حذف المحتوى بنجاح', 'success')
        schedule_posts()
    except Exception as e:
        logger.error(f"Error deleting content: {e}")
        flash(f'حدث خطأ أثناء حذف المحتوى: {str(e)}', 'error')
    return redirect(url_for('admin_panel'))

# ==================== Admin: Channels ====================
@app.route('/admin/channel/delete/<int:channel_id>')
@admin_required
def admin_delete_channel(channel_id):
    try:
        with get_db() as cur:
            for job in scheduler.get_jobs():
                if job.id.startswith(f'publish_{channel_id}_'):
                    scheduler.remove_job(job.id)
            cur.execute("DELETE FROM publish_channels WHERE id = %s", (channel_id,))
        flash('تم حذف القناة بنجاح', 'success')
    except Exception as e:
        logger.error(f"Error deleting channel: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/channel/toggle/<int:channel_id>')
@admin_required
def admin_toggle_channel(channel_id):
    try:
        with get_db() as cur:
            cur.execute("SELECT is_active FROM publish_channels WHERE id = %s", (channel_id,))
            row = cur.fetchone()
            if row:
                new_status = not row['is_active']
                cur.execute("UPDATE publish_channels SET is_active = %s WHERE id = %s", (new_status, channel_id))
                if new_status:
                    schedule_posts()
                else:
                    for job in scheduler.get_jobs():
                        if job.id.startswith(f'publish_{channel_id}_'):
                            scheduler.remove_job(job.id)
                flash('تم تحديث حالة القناة', 'success')
    except Exception as e:
        logger.error(f"Error toggling channel: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

# ==================== Admin: Ads ====================
@app.route('/admin/ad/add', methods=['POST'])
@admin_required
def admin_add_ad():
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    link = request.form.get('link', '').strip()
    is_active = request.form.get('is_active') == 'on'
    
    if not title or not content:
        flash('العنوان والنص مطلوبان', 'error')
        return redirect(url_for('admin_panel'))
    
    try:
        with get_db() as cur:
            cur.execute(
                "INSERT INTO ads (title, content, link, is_active) VALUES (%s, %s, %s, %s)",
                (title, content, link or None, is_active)
            )
        flash('تم إضافة الإعلان بنجاح', 'success')
    except Exception as e:
        logger.error(f"Error adding ad: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/ad/<int:ad_id>/edit', methods=['POST'])
@admin_required
def admin_edit_ad(ad_id):
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    link = request.form.get('link', '').strip()
    is_active = request.form.get('is_active') == 'on'
    
    if not title or not content:
        flash('العنوان والنص مطلوبان', 'error')
        return redirect(url_for('admin_panel'))
    
    try:
        with get_db() as cur:
            cur.execute(
                "UPDATE ads SET title = %s, content = %s, link = %s, is_active = %s WHERE id = %s",
                (title, content, link or None, is_active, ad_id)
            )
        flash('تم تعديل الإعلان بنجاح', 'success')
    except Exception as e:
        logger.error(f"Error editing ad: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/ad/<int:ad_id>/delete')
@admin_required
def admin_delete_ad(ad_id):
    try:
        with get_db() as cur:
            cur.execute("DELETE FROM ads WHERE id = %s", (ad_id,))
        flash('تم حذف الإعلان بنجاح', 'success')
    except Exception as e:
        logger.error(f"Error deleting ad: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/ad/<int:ad_id>/toggle')
@admin_required
def admin_toggle_ad(ad_id):
    try:
        with get_db() as cur:
            cur.execute("SELECT is_active FROM ads WHERE id = %s", (ad_id,))
            row = cur.fetchone()
            if row:
                new_status = not row['is_active']
                cur.execute("UPDATE ads SET is_active = %s WHERE id = %s", (new_status, ad_id))
                flash(f'تم {"تفعيل" if new_status else "إيقاف"} الإعلان', 'success')
    except Exception as e:
        logger.error(f"Error toggling ad: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

# ==================== API ====================
@app.route('/api/characters')
def api_characters():
    now = time_module.time()
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
        return jsonify({'error': str(e)}), 500
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
        'temperature': 0.7
    }
    try:
        response = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=30)
        result = response.json()
        return jsonify({'response': result['choices'][0]['message']['content']})
    except Exception as e:
        logger.error(f"API chat error: {e}")
        return jsonify({'error': str(e)}), 500

# ==================== بدء التشغيل ====================
if __name__ == '__main__':
    init_db()
    schedule_posts()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False)
