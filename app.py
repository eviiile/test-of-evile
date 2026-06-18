import os
import logging
import requests
import time
import json
from datetime import datetime, timedelta
from functools import wraps
from contextlib import contextmanager
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
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

def ensure_channel_columns(cur):
    cur.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name='channels' AND column_name='is_paused'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE channels ADD COLUMN is_paused BOOLEAN DEFAULT FALSE")
        logger.info("Added column is_paused to channels table")

def ensure_content_columns(cur):
    cur.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name='content_templates' AND column_name='publish_time'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE content_templates ADD COLUMN publish_time TIMESTAMP DEFAULT NULL")
        logger.info("Added column publish_time to content_templates table")

def fix_publish_settings():
    """إصلاح بيانات JSON التالفة في publish_settings"""
    try:
        with get_db() as cur:
            cur.execute("SELECT id, publish_times FROM publish_settings LIMIT 1")
            row = cur.fetchone()
            if row:
                raw = row['publish_times']
                try:
                    json.loads(raw)  # محاولة التحقق من الصحة
                except json.JSONDecodeError:
                    default = '["09:00","13:00","17:00"]'
                    cur.execute("UPDATE publish_settings SET publish_times = %s WHERE id = %s", (default, row['id']))
                    logger.warning(f"Fixed invalid publish_times: {raw[:50]}... -> {default}")
    except Exception as e:
        logger.error(f"Error fixing publish settings: {e}")

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
            cur.execute('''CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                channel_id TEXT UNIQUE NOT NULL,
                channel_username TEXT,
                admin_id TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                is_paused BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_post_at TIMESTAMP
            )''')
            cur.execute('''CREATE TABLE IF NOT EXISTS channel_failures (
                id SERIAL PRIMARY KEY,
                channel_id TEXT NOT NULL,
                reason TEXT,
                failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            cur.execute('''CREATE TABLE IF NOT EXISTS publish_settings (
                id SERIAL PRIMARY KEY,
                publish_count INTEGER DEFAULT 3,
                publish_times TEXT DEFAULT '["09:00","13:00","17:00"]'
            )''')
            cur.execute('''CREATE TABLE IF NOT EXISTS published_posts (
                id SERIAL PRIMARY KEY,
                channel_id TEXT NOT NULL,
                content TEXT NOT NULL,
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                message_id TEXT
            )''')
            cur.execute('''CREATE TABLE IF NOT EXISTS content_templates (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                prompt TEXT,
                content TEXT NOT NULL,
                publish_time TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            cur.execute('''CREATE TABLE IF NOT EXISTS scheduled_content (
                id SERIAL PRIMARY KEY,
                content_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                scheduled_time TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            ensure_channel_columns(cur)
            ensure_content_columns(cur)
            
            cur.execute("SELECT COUNT(*) FROM publish_settings")
            if cur.fetchone()['count'] == 0:
                cur.execute("INSERT INTO publish_settings (publish_count, publish_times) VALUES (3, '[\"09:00\",\"13:00\",\"17:00\"]')")
            
            ensure_notification_columns(cur)
            fix_publish_settings()  # إصلاح البيانات التالفة
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

def get_publish_times():
    try:
        with get_db() as cur:
            cur.execute("SELECT publish_times FROM publish_settings LIMIT 1")
            row = cur.fetchone()
            if row:
                try:
                    times = json.loads(row['publish_times'])
                    if isinstance(times, list) and times:
                        return times
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON in publish_times: {row['publish_times']}")
    except Exception as e:
        logger.error(f"Error getting publish times: {e}")
    return ["09:00", "13:00", "17:00"]

# ==================== توليد المحتوى ====================
def generate_post_content(content_id=None, custom_prompt=None):
    prompt = None
    
    if content_id:
        with get_db() as cur:
            cur.execute("SELECT prompt FROM content_templates WHERE id = %s", (content_id,))
            row = cur.fetchone()
            if row and row['prompt']:
                prompt = row['prompt']
    
    if not prompt and custom_prompt:
        prompt = custom_prompt
    
    if not prompt:
        prompt = """أنت الآن كاتب محتوى تقني لقناة تلغرام، مهمتك: توليد مقالة قصيرة جداً (بين 100 إلى 150 كلمة) بشكل عشوائي فوري، على أن تنتقي عشوائياً موضوعاً واحداً فقط حصراً من القائمة التالية: (الأمن السيبراني، لغات البرمجة مثل Rust أو Zig، مشاريع ساخنة على GitHub، منصات عالمية مثل AWS أو Cloudflare، نماذج الذكاء الاصطناعي الجديدة)، وتلتزم بهذا الموضوع الواحد بسياق سردي واحد متصل دون أي تشعب أو دمج مع مواضيع أخرى، مع أسلوب كتابة مشوق للغاية يجذب القارئ من أول جملة عبر البدء بتساؤل أو مفارقة أو حقيقة صادمة، مع الحفاظ على التدفق السردي المتصل دون أي عناوين فرعية أو نقاط تعداد أو إيموجي، واستخدم صياغة حوارية احترافية مختصرة، وقبل الصياغة نفذ بحثاً متعمقاً للتحقق من الأرقام والإصدارات والأخبار، وعند ذكر أي أداة أو مشروع أو منصة ادمج رابطها الرسمي بصيغة Markdown الخاصة بتلغرام [النص](الرابط) لتكون قابلة للنقر، وتجنب تماماً الوعود المبالغ فيها، واكتب المقالة الآن في ردك الأول دون انتظار مني."""
    
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
        logger.debug("Generating post content via OpenRouter (GPT-4o-mini)")
        response = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=60)
        result = response.json()
        
        if result and 'choices' in result and len(result['choices']) > 0:
            message = result['choices'][0].get('message', {})
            content = message.get('content')
            if content:
                logger.debug("Content generated successfully")
                return content.strip()
        
        logger.error(f"Invalid response from OpenRouter: {result}")
        return None
    except Exception as e:
        logger.error(f"Error generating content: {e}")
        return None

# ==================== النشر في تلغرام ====================
def send_telegram_message(channel_id, text):
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set")
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': channel_id,
        'text': text,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': False
    }
    try:
        logger.debug(f"Sending message to channel {channel_id}")
        response = requests.post(url, json=payload, timeout=30)
        data = response.json()
        if data.get('ok'):
            logger.info(f"Message sent successfully to {channel_id}")
            return data['result']['message_id']
        else:
            logger.error(f"Telegram API error: {data}")
            return None
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return None

# ==================== جدولة النشر ====================
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()

def schedule_posts_for_channel(channel_id, admin_id):
    for job in scheduler.get_jobs():
        if job.id.startswith(f'publish_{channel_id}_') or job.id.startswith(f'content_'):
            scheduler.remove_job(job.id)
    
    times = get_publish_times()
    for time_str in times:
        hour, minute = map(int, time_str.split(':'))
        job_id = f'publish_{channel_id}_{hour}_{minute}'
        trigger = CronTrigger(hour=hour, minute=minute, timezone=TIMEZONE)
        scheduler.add_job(
            func=publish_scheduled_post,
            trigger=trigger,
            id=job_id,
            args=[channel_id],
            replace_existing=True
        )
        logger.info(f"Scheduled daily post for channel {channel_id} at {time_str} (Asia/Aden)")
    
    schedule_content_templates(channel_id)

def schedule_content_templates(channel_id):
    with get_db() as cur:
        cur.execute("""
            SELECT ct.id, ct.content, ct.prompt, ct.publish_time 
            FROM content_templates ct
            LEFT JOIN scheduled_content sc ON sc.content_id = ct.id AND sc.channel_id = %s
            WHERE ct.publish_time IS NOT NULL 
              AND ct.publish_time > NOW()
              AND sc.id IS NULL
        """, (channel_id,))
        contents = cur.fetchall()
        
        for content in contents:
            scheduled_time = content['publish_time']
            if scheduled_time.tzinfo is None:
                scheduled_time = TIMEZONE.localize(scheduled_time)
            else:
                scheduled_time = scheduled_time.astimezone(TIMEZONE)
            
            cur.execute(
                "INSERT INTO scheduled_content (content_id, channel_id, scheduled_time) VALUES (%s, %s, %s)",
                (content['id'], channel_id, scheduled_time)
            )
            
            job_id = f'content_{content["id"]}_{channel_id}'
            scheduler.add_job(
                func=publish_content_job,
                trigger=DateTrigger(run_date=scheduled_time, timezone=TIMEZONE),
                id=job_id,
                args=[content['id'], channel_id, content['content']],
                replace_existing=True
            )
            logger.info(f"Scheduled content {content['id']} for channel {channel_id} at {scheduled_time}")

def publish_scheduled_post(channel_id):
    with get_db() as cur:
        cur.execute("SELECT is_paused, is_active FROM channels WHERE channel_id = %s", (channel_id,))
        row = cur.fetchone()
        if not row or not row['is_active'] or row['is_paused']:
            logger.info(f"Channel {channel_id} is paused or inactive, skipping daily post")
            return
    
    with get_db() as cur:
        cur.execute("SELECT id, prompt FROM content_templates WHERE prompt IS NOT NULL AND prompt != '' ORDER BY RANDOM() LIMIT 1")
        template = cur.fetchone()
        if template:
            content = generate_post_content(content_id=template['id'])
        else:
            content = generate_post_content()
    
    if not content:
        with get_db() as cur:
            cur.execute("INSERT INTO channel_failures (channel_id, reason) VALUES (%s, %s)", (channel_id, 'فشل توليد المحتوى اليومي'))
        logger.error(f"Failed to generate content for channel {channel_id}")
        return
    
    message_id = send_telegram_message(channel_id, content)
    if message_id:
        with get_db() as cur:
            cur.execute("INSERT INTO published_posts (channel_id, content, message_id) VALUES (%s, %s, %s)", (channel_id, content, message_id))
            cur.execute("UPDATE channels SET last_post_at = NOW() WHERE channel_id = %s", (channel_id,))
        logger.info(f"Published daily post to channel {channel_id}")
    else:
        with get_db() as cur:
            cur.execute("INSERT INTO channel_failures (channel_id, reason) VALUES (%s, %s)", (channel_id, 'فشل إرسال الرسالة إلى تلغرام'))
        logger.error(f"Failed to publish daily post to channel {channel_id}")

def publish_content_job(content_id, channel_id, content):
    with get_db() as cur:
        cur.execute("SELECT is_paused, is_active FROM channels WHERE channel_id = %s", (channel_id,))
        row = cur.fetchone()
        if not row or not row['is_active'] or row['is_paused']:
            logger.info(f"Channel {channel_id} is paused or inactive, skipping scheduled content {content_id}")
            cur.execute("UPDATE scheduled_content SET status = 'cancelled' WHERE content_id = %s AND channel_id = %s", (content_id, channel_id))
            return
        
        cur.execute("UPDATE scheduled_content SET status = 'published' WHERE content_id = %s AND channel_id = %s", (content_id, channel_id))
    
    message_id = send_telegram_message(channel_id, content)
    if message_id:
        with get_db() as cur:
            cur.execute("INSERT INTO published_posts (channel_id, content, message_id) VALUES (%s, %s, %s)", (channel_id, content, message_id))
            cur.execute("UPDATE channels SET last_post_at = NOW() WHERE channel_id = %s", (channel_id,))
            cur.execute("DELETE FROM scheduled_content WHERE content_id = %s AND channel_id = %s", (content_id, channel_id))
        logger.info(f"Published scheduled content {content_id} to channel {channel_id}")
    else:
        with get_db() as cur:
            cur.execute("UPDATE scheduled_content SET status = 'failed' WHERE content_id = %s AND channel_id = %s", (content_id, channel_id))
            cur.execute("INSERT INTO channel_failures (channel_id, reason) VALUES (%s, %s)", (channel_id, f'فشل نشر المحتوى {content_id}'))
        logger.error(f"Failed to publish scheduled content {content_id} to channel {channel_id}")

def schedule_all_channels():
    try:
        with get_db() as cur:
            cur.execute("SELECT channel_id FROM channels WHERE is_active = true AND is_paused = false")
            rows = cur.fetchall()
            for row in rows:
                schedule_posts_for_channel(row['channel_id'], None)
            logger.info(f"Scheduled {len(rows)} channels")
    except Exception as e:
        logger.error(f"Error scheduling all channels: {e}")

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

@app.route('/publish')
def publish():
    telegram_id = session.get('telegram_id')
    if not telegram_id:
        return redirect(url_for('index'))
    
    with get_db() as cur:
        cur.execute("SELECT * FROM channels WHERE admin_id = %s", (telegram_id,))
        channel = cur.fetchone()
        
        if not channel:
            return render_template('publish.html',
                                 channel=None,
                                 posts_count=0,
                                 recent_posts=[],
                                 times=get_publish_times(),
                                 members_count=0,
                                 is_paused=False,
                                 content_templates=[],
                                 selected_content_id=None,
                                 telegram_id=telegram_id)
        
        cur.execute("""
            SELECT ct.*, 
                   CASE WHEN sc.id IS NOT NULL THEN 'scheduled' ELSE 'available' END as status
            FROM content_templates ct
            LEFT JOIN scheduled_content sc ON sc.content_id = ct.id AND sc.channel_id = %s
            WHERE ct.publish_time IS NULL OR ct.publish_time > NOW()
            ORDER BY ct.created_at DESC
        """, (channel['channel_id'],))
        content_templates = cur.fetchall()
        
        cur.execute("SELECT COUNT(*) FROM published_posts WHERE channel_id = %s", (channel['channel_id'],))
        posts_count = cur.fetchone()['count']
        
        cur.execute("SELECT content, published_at FROM published_posts WHERE channel_id = %s ORDER BY published_at DESC LIMIT 5", (channel['channel_id'],))
        recent_posts = cur.fetchall()
        
        times = get_publish_times()
        
        members_count = 0
        if BOT_TOKEN:
            try:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMembersCount?chat_id={channel['channel_id']}"
                resp = requests.get(url, timeout=10)
                data = resp.json()
                if data.get('ok'):
                    members_count = data['result']
            except Exception as e:
                logger.error(f"Error getting members count: {e}")
        
        is_paused = channel.get('is_paused', False)
        
        return render_template('publish.html',
                             channel=channel,
                             posts_count=posts_count,
                             recent_posts=recent_posts,
                             times=times,
                             members_count=members_count,
                             is_paused=is_paused,
                             content_templates=content_templates,
                             selected_content_id=None,
                             telegram_id=telegram_id)

@app.route('/publish/register_channel', methods=['POST'])
def register_channel():
    telegram_id = session.get('telegram_id')
    if not telegram_id:
        return jsonify({'success': False, 'message': 'يجب تسجيل الدخول أولاً'}), 401
    
    channel_id = request.form.get('channel_id', '').strip()
    channel_username = request.form.get('channel_username', '').strip()
    
    if not channel_id:
        return jsonify({'success': False, 'message': 'معرف القناة مطلوب'}), 400
    
    if channel_username and not channel_username.startswith('@'):
        return jsonify({'success': False, 'message': 'اسم المستخدم يجب أن يبدأ بـ @'}), 400
    
    try:
        with get_db() as cur:
            cur.execute("SELECT id FROM channels WHERE admin_id = %s", (telegram_id,))
            if cur.fetchone():
                return jsonify({'success': False, 'message': 'لديك قناة مسجلة مسبقاً'}), 400
            
            cur.execute("SELECT id FROM channels WHERE channel_id = %s", (channel_id,))
            if cur.fetchone():
                return jsonify({'success': False, 'message': 'هذه القناة مسجلة مسبقاً'}), 400
            
            cur.execute(
                "INSERT INTO channels (channel_id, channel_username, admin_id, is_active, is_paused) VALUES (%s, %s, %s, true, false)",
                (channel_id, channel_username, telegram_id)
            )
            logger.info(f"Channel {channel_id} registered successfully for user {telegram_id}")
        
        schedule_posts_for_channel(channel_id, telegram_id)
        return jsonify({'success': True, 'message': 'تم تسجيل القناة بنجاح'})
    except Exception as e:
        logger.error(f"Error registering channel: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/publish/toggle_pause')
def toggle_pause():
    telegram_id = session.get('telegram_id')
    if not telegram_id:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    try:
        with get_db() as cur:
            cur.execute("SELECT id, channel_id, is_paused FROM channels WHERE admin_id = %s", (telegram_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'message': 'لا توجد قناة مسجلة'}), 400
            
            new_status = not row['is_paused']
            cur.execute("UPDATE channels SET is_paused = %s WHERE id = %s", (new_status, row['id']))
            
            if new_status:
                for job in scheduler.get_jobs():
                    if job.id.startswith(f'publish_{row["channel_id"]}_') or job.id.startswith(f'content_'):
                        scheduler.remove_job(job.id)
                logger.info(f"Paused publishing for channel {row['channel_id']}")
            else:
                schedule_posts_for_channel(row['channel_id'], telegram_id)
                logger.info(f"Resumed publishing for channel {row['channel_id']}")
        
        return jsonify({'success': True, 'is_paused': new_status})
    except Exception as e:
        logger.error(f"Error toggling pause: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/publish/stop')
def stop_publishing():
    telegram_id = session.get('telegram_id')
    if not telegram_id:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    try:
        with get_db() as cur:
            cur.execute("SELECT id, channel_id FROM channels WHERE admin_id = %s", (telegram_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'message': 'لا توجد قناة مسجلة'}), 400
            
            for job in scheduler.get_jobs():
                if job.id.startswith(f'publish_{row["channel_id"]}_') or job.id.startswith(f'content_'):
                    scheduler.remove_job(job.id)
            
            cur.execute("UPDATE channels SET is_active = false WHERE id = %s", (row['id'],))
            logger.info(f"Stopped publishing for channel {row['channel_id']}")
        
        return jsonify({'success': True, 'message': 'تم إيقاف النشر نهائياً'})
    except Exception as e:
        logger.error(f"Error stopping publishing: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/publish/force_publish')
def force_publish():
    telegram_id = session.get('telegram_id')
    if not telegram_id:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    try:
        with get_db() as cur:
            cur.execute("SELECT channel_id FROM channels WHERE admin_id = %s AND is_active = true", (telegram_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'message': 'لا توجد قناة نشطة'}), 400
            channel_id = row['channel_id']
        
        with get_db() as cur:
            cur.execute("SELECT id, prompt FROM content_templates WHERE prompt IS NOT NULL AND prompt != '' ORDER BY RANDOM() LIMIT 1")
            template = cur.fetchone()
            if template:
                content = generate_post_content(content_id=template['id'])
            else:
                content = generate_post_content()
        
        if not content:
            return jsonify({'success': False, 'message': 'فشل توليد المحتوى'}), 500
        
        message_id = send_telegram_message(channel_id, content)
        if message_id:
            with get_db() as cur:
                cur.execute("INSERT INTO published_posts (channel_id, content, message_id) VALUES (%s, %s, %s)", (channel_id, content, message_id))
                cur.execute("UPDATE channels SET last_post_at = NOW() WHERE channel_id = %s", (channel_id,))
            logger.info(f"Forced publish to channel {channel_id}")
            return jsonify({'success': True, 'message': 'تم النشر بنجاح'})
        else:
            return jsonify({'success': False, 'message': 'فشل النشر'}), 500
    except Exception as e:
        logger.error(f"Error in force publish: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ==================== إدارة المحتوى (Admin) ====================
@app.route('/admin/content/generate', methods=['POST'])
@admin_required
def generate_content_ai():
    prompt = request.form.get('prompt', '').strip()
    if not prompt:
        return jsonify({'success': False, 'message': 'البرومبت مطلوب'}), 400
    
    content = generate_post_content(custom_prompt=prompt)
    if content:
        return jsonify({'success': True, 'content': content})
    else:
        return jsonify({'success': False, 'message': 'فشل توليد المحتوى'}), 500

@app.route('/admin/content/add', methods=['POST'])
@admin_required
def add_content():
    name = request.form.get('name', '').strip()
    prompt = request.form.get('prompt', '').strip()
    content = request.form.get('content', '').strip()
    publish_time_str = request.form.get('publish_time', '').strip()
    
    if not name or not content:
        flash('اسم المحتوى والمحتوى نفسه مطلوبان', 'error')
        return redirect(url_for('admin_panel'))
    
    publish_time = None
    if publish_time_str:
        try:
            publish_time = datetime.fromisoformat(publish_time_str)
            publish_time = TIMEZONE.localize(publish_time)
        except ValueError as e:
            flash(f'تنسيق الوقت غير صحيح: {e}', 'error')
            return redirect(url_for('admin_panel'))
    
    try:
        with get_db() as cur:
            cur.execute(
                "INSERT INTO content_templates (name, prompt, content, publish_time) VALUES (%s, %s, %s, %s)",
                (name, prompt, content, publish_time)
            )
            content_id = cur.fetchone()['id'] if hasattr(cur, 'fetchone') else None
            
            if publish_time and content_id:
                cur.execute("SELECT channel_id FROM channels WHERE is_active = true AND is_paused = false")
                channels = cur.fetchall()
                for ch in channels:
                    schedule_content_for_channel(content_id, ch['channel_id'], content, publish_time)
        
        flash('تم إضافة المحتوى بنجاح', 'success')
    except Exception as e:
        logger.error(f"Error adding content: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

def schedule_content_for_channel(content_id, channel_id, content, publish_time):
    with get_db() as cur:
        cur.execute(
            "INSERT INTO scheduled_content (content_id, channel_id, scheduled_time) VALUES (%s, %s, %s)",
            (content_id, channel_id, publish_time)
        )
    
    job_id = f'content_{content_id}_{channel_id}'
    scheduler.add_job(
        func=publish_content_job,
        trigger=DateTrigger(run_date=publish_time, timezone=TIMEZONE),
        id=job_id,
        args=[content_id, channel_id, content],
        replace_existing=True
    )
    logger.info(f"Scheduled content {content_id} for channel {channel_id} at {publish_time}")

@app.route('/admin/content/<int:content_id>/edit', methods=['POST'])
@admin_required
def edit_content(content_id):
    name = request.form.get('name', '').strip()
    prompt = request.form.get('prompt', '').strip()
    content = request.form.get('content', '').strip()
    publish_time_str = request.form.get('publish_time', '').strip()
    
    if not name or not content:
        flash('اسم المحتوى والمحتوى نفسه مطلوبان', 'error')
        return redirect(url_for('admin_panel'))
    
    publish_time = None
    if publish_time_str:
        try:
            publish_time = datetime.fromisoformat(publish_time_str)
            publish_time = TIMEZONE.localize(publish_time)
        except ValueError as e:
            flash(f'تنسيق الوقت غير صحيح: {e}', 'error')
            return redirect(url_for('admin_panel'))
    
    try:
        with get_db() as cur:
            cur.execute("SELECT publish_time FROM content_templates WHERE id = %s", (content_id,))
            old = cur.fetchone()
            old_time = old['publish_time'] if old else None
            
            cur.execute(
                "UPDATE content_templates SET name = %s, prompt = %s, content = %s, publish_time = %s WHERE id = %s",
                (name, prompt, content, publish_time, content_id)
            )
            
            if old_time != publish_time:
                for job in scheduler.get_jobs():
                    if job.id.startswith(f'content_{content_id}_'):
                        scheduler.remove_job(job.id)
                
                cur.execute("DELETE FROM scheduled_content WHERE content_id = %s", (content_id,))
                
                if publish_time:
                    cur.execute("SELECT channel_id FROM channels WHERE is_active = true AND is_paused = false")
                    channels = cur.fetchall()
                    for ch in channels:
                        schedule_content_for_channel(content_id, ch['channel_id'], content, publish_time)
        
        flash('تم تعديل المحتوى بنجاح', 'success')
    except Exception as e:
        logger.error(f"Error editing content: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/content/<int:content_id>/delete')
@admin_required
def delete_content(content_id):
    try:
        with get_db() as cur:
            for job in scheduler.get_jobs():
                if job.id.startswith(f'content_{content_id}_'):
                    scheduler.remove_job(job.id)
            cur.execute("DELETE FROM scheduled_content WHERE content_id = %s", (content_id,))
            cur.execute("DELETE FROM content_templates WHERE id = %s", (content_id,))
        flash('تم حذف المحتوى بنجاح', 'success')
    except Exception as e:
        logger.error(f"Error deleting content: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

# ==================== بقية مسارات الإدارة ====================
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
            cur.execute('SELECT * FROM channels ORDER BY created_at DESC')
            all_channels = cur.fetchall()
            cur.execute('''SELECT cf.*, c.channel_username 
                           FROM channel_failures cf 
                           LEFT JOIN channels c ON cf.channel_id = c.channel_id 
                           ORDER BY cf.failed_at DESC''')
            failures = cur.fetchall()
            cur.execute("SELECT publish_count, publish_times FROM publish_settings LIMIT 1")
            settings = cur.fetchone()
            if settings:
                publish_count = settings['publish_count']
                try:
                    publish_times = json.loads(settings['publish_times'])
                except json.JSONDecodeError:
                    publish_times = ["09:00", "13:00", "17:00"]
            else:
                publish_count = 3
                publish_times = ["09:00", "13:00", "17:00"]
            cur.execute("SELECT * FROM content_templates ORDER BY id DESC")
            content_templates = cur.fetchall()
    except Exception as e:
        logger.error(f"Admin panel error: {e}")
        characters, notifications, users_count = [], [], 0
        all_channels = failures = []
        publish_count = 3
        publish_times = ["09:00", "13:00", "17:00"]
        content_templates = []
    
    return render_template('admin.html',
                         logged_in=True,
                         characters=characters,
                         notifications=notifications,
                         users_count=users_count,
                         all_channels=all_channels,
                         failures=failures,
                         publish_count=publish_count,
                         publish_times=publish_times,
                         content_templates=content_templates)

# ==================== بقية مسارات الإدارة ====================
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

@app.route('/admin/channel/add', methods=['POST'])
@admin_required
def admin_add_channel():
    channel_id = request.form.get('channel_id', '').strip()
    channel_username = request.form.get('channel_username', '').strip()
    admin_id = request.form.get('admin_id', '').strip()
    
    if not channel_id or not admin_id:
        flash('معرف القناة ومعرف المشرف مطلوبان', 'error')
        return redirect(url_for('admin_panel'))
    
    try:
        with get_db() as cur:
            cur.execute(
                "INSERT INTO channels (channel_id, channel_username, admin_id, is_active, is_paused) VALUES (%s, %s, %s, true, false)",
                (channel_id, channel_username, admin_id)
            )
        flash('تمت إضافة القناة بنجاح', 'success')
    except Exception as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/channel/delete/<int:channel_id>')
@admin_required
def admin_delete_channel(channel_id):
    try:
        with get_db() as cur:
            cur.execute("DELETE FROM channels WHERE id=%s", (channel_id,))
        flash('تم حذف القناة', 'success')
    except Exception as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/channel/toggle/<int:channel_id>')
@admin_required
def admin_toggle_channel(channel_id):
    try:
        with get_db() as cur:
            cur.execute("SELECT is_active FROM channels WHERE id=%s", (channel_id,))
            row = cur.fetchone()
            if row:
                new_status = not row['is_active']
                cur.execute("UPDATE channels SET is_active=%s WHERE id=%s", (new_status, channel_id))
                if new_status:
                    cur.execute("SELECT channel_id FROM channels WHERE id=%s", (channel_id,))
                    ch = cur.fetchone()
                    if ch:
                        schedule_posts_for_channel(ch['channel_id'], None)
                else:
                    cur.execute("SELECT channel_id FROM channels WHERE id=%s", (channel_id,))
                    ch = cur.fetchone()
                    if ch:
                        for job in scheduler.get_jobs():
                            if job.id.startswith(f'publish_{ch["channel_id"]}_') or job.id.startswith(f'content_'):
                                scheduler.remove_job(job.id)
                flash('تم تحديث حالة القناة', 'success')
    except Exception as e:
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/publish/settings', methods=['POST'])
@admin_required
def save_publish_settings():
    try:
        publish_count = int(request.form.get('publish_count', 3))
        times = request.form.getlist('publish_time[]')
        if not times:
            times = request.form.get('publish_time', '').split(',')
        times = [t.strip() for t in times if t.strip()]
        if len(times) != publish_count:
            if len(times) > publish_count:
                times = times[:publish_count]
            else:
                default_times = ["09:00", "13:00", "17:00", "20:00", "22:00"]
                for i in range(len(times), publish_count):
                    times.append(default_times[i] if i < len(default_times) else "12:00")
        times_json = json.dumps(times)
        with get_db() as cur:
            cur.execute("UPDATE publish_settings SET publish_count = %s, publish_times = %s", (publish_count, times_json))
        schedule_all_channels()
        flash('تم حفظ إعدادات النشر وإعادة جدولة القنوات', 'success')
    except Exception as e:
        logger.error(f"Error saving publish settings: {e}")
        flash(f'حدث خطأ: {str(e)}', 'error')
    return redirect(url_for('admin_panel'))

# ==================== API ====================
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
    schedule_all_channels()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False)
