import os
import logging
import requests
import json
import secrets
import string
from datetime import datetime, timedelta
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
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', 'sk-or-v1-...')  # ضع مفتاحك
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DATABASE_URL = "postgresql://evile_site_user:yxWlZVZsC39DhRtXoY7e84ci6NTJgcaR@dpg-d8mpl3rsq97s739pscq0-a.oregon-postgres.render.com/evile_site"

BOT_TOKEN = os.getenv('BOT_TOKEN', '')
TIMEZONE = pytz.timezone('Asia/Aden')

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

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
        logger.error(f"Database error: {e}")
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def init_db():
    """إنشاء الجداول إذا لم تكن موجودة وإدراج البيانات الافتراضية"""
    with get_db() as cur:
        # جداول المستخدمين والقنوات والمحتوى...
        cur.execute('''CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            user_code TEXT UNIQUE NOT NULL,
            is_premium BOOLEAN DEFAULT FALSE,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS channels (
            id SERIAL PRIMARY KEY,
            channel_id TEXT UNIQUE NOT NULL,
            channel_username TEXT,
            user_code TEXT NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_post_at TIMESTAMP
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS content_templates (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            prompt TEXT,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS published_posts (
            id SERIAL PRIMARY KEY,
            channel_id TEXT NOT NULL,
            content TEXT NOT NULL,
            published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            message_id TEXT
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS channel_failures (
            id SERIAL PRIMARY KEY,
            channel_id TEXT NOT NULL,
            reason TEXT,
            failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS publish_settings (
            id SERIAL PRIMARY KEY,
            publish_times TEXT DEFAULT '12:00'
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        # إدراج إعدادات النشر الافتراضية إذا لم توجد
        cur.execute("SELECT COUNT(*) FROM publish_settings")
        if cur.fetchone()['count'] == 0:
            cur.execute("INSERT INTO publish_settings (publish_times) VALUES ('12:00')")
        # تحديث أي قيمة JSON قديمة في publish_times
        cur.execute("SELECT publish_times FROM publish_settings LIMIT 1")
        row = cur.fetchone()
        if row:
            val = row['publish_times']
            if val and val.startswith('['):
                try:
                    times = json.loads(val)
                    if times and isinstance(times, list):
                        cur.execute("UPDATE publish_settings SET publish_times = %s", (times[0],))
                except:
                    pass
        # إدراج محتوى افتراضي إذا لم يوجد
        cur.execute("SELECT COUNT(*) FROM content_templates")
        if cur.fetchone()['count'] == 0:
            cur.execute("INSERT INTO content_templates (name, prompt, content) VALUES (%s, %s, %s)",
                        ('افتراضي', 'أنت كاتب محتوى تقني...', 'محتوى نموذجي'))
    logger.info("Database initialized")

# ==================== دوال مساعدة ====================
def generate_user_code():
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(8))

def get_publish_time():
    try:
        with get_db() as cur:
            cur.execute("SELECT publish_times FROM publish_settings LIMIT 1")
            row = cur.fetchone()
            if not row:
                return '12:00'
            val = row['publish_times']
            if val and val.startswith('['):
                try:
                    times = json.loads(val)
                    if times and isinstance(times, list):
                        return times[0]
                except:
                    pass
            return val if val else '12:00'
    except Exception as e:
        logger.error(f"Error getting publish time: {e}")
        return '12:00'

def generate_post_content(content_id=None, custom_prompt=None):
    prompt = None
    if content_id:
        try:
            with get_db() as cur:
                cur.execute("SELECT prompt FROM content_templates WHERE id = %s", (content_id,))
                row = cur.fetchone()
                if row and row['prompt']:
                    prompt = row['prompt']
        except:
            pass
    if not prompt and custom_prompt:
        prompt = custom_prompt
    if not prompt:
        prompt = "أنت كاتب محتوى تقني... (برومبت افتراضي)"
    headers = {'Authorization': f'Bearer {OPENROUTER_API_KEY}', 'Content-Type': 'application/json'}
    payload = {
        'model': 'openai/gpt-4o-mini',
        'messages': [{'role': 'system', 'content': 'أنت كاتب محتوى تقني محترف.'},
                     {'role': 'user', 'content': prompt}],
        'temperature': 0.9,
        'max_tokens': 400
    }
    try:
        response = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=60)
        result = response.json()
        if result and 'choices' in result:
            return result['choices'][0]['message']['content'].strip()
    except Exception as e:
        logger.error(f"Generate error: {e}")
    return None

def send_telegram_message(channel_id, text):
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {'chat_id': channel_id, 'text': text, 'parse_mode': 'Markdown'}
    try:
        response = requests.post(url, json=payload, timeout=30)
        data = response.json()
        if data.get('ok'):
            return data['result']['message_id']
    except Exception as e:
        logger.error(f"Send error: {e}")
    return None

# ==================== جدولة النشر اليومي ====================
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()

def publish_daily_job():
    try:
        with get_db() as cur:
            cur.execute("SELECT channel_id FROM channels WHERE is_active = true")
            channels = cur.fetchall()
            for ch in channels:
                cur.execute("SELECT id, prompt FROM content_templates ORDER BY RANDOM() LIMIT 1")
                template = cur.fetchone()
                content = generate_post_content(content_id=template['id']) if template else generate_post_content()
                if content:
                    msg_id = send_telegram_message(ch['channel_id'], content)
                    if msg_id:
                        cur.execute("INSERT INTO published_posts (channel_id, content, message_id) VALUES (%s, %s, %s)",
                                    (ch['channel_id'], content, msg_id))
                        cur.execute("UPDATE channels SET last_post_at = NOW() WHERE channel_id = %s", (ch['channel_id'],))
                    else:
                        cur.execute("INSERT INTO channel_failures (channel_id, reason) VALUES (%s, %s)",
                                    (ch['channel_id'], 'فشل الإرسال'))
    except Exception as e:
        logger.error(f"Publish daily job error: {e}")

def schedule_daily_job():
    for job in scheduler.get_jobs():
        if job.id == 'daily_publish':
            scheduler.remove_job(job.id)
    time_str = get_publish_time()
    try:
        hour, minute = map(int, time_str.split(':'))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except:
        hour, minute = 12, 0
    trigger = CronTrigger(hour=hour, minute=minute, timezone=TIMEZONE)
    scheduler.add_job(publish_daily_job, trigger, id='daily_publish', replace_existing=True)
    logger.info(f"Scheduled daily publish at {hour:02d}:{minute:02d}")

# ==================== Routes ====================
@app.route('/')
def index():
    user_code = session.get('user_code')
    latest_notification = None
    try:
        with get_db() as cur:
            cur.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 1")
            latest_notification = cur.fetchone()
    except Exception as e:
        logger.error(f"Index error: {e}")
    return render_template('index.html', user_code=user_code, latest_notification=latest_notification)

@app.route('/register', methods=['POST'])
def register():
    user_code = generate_user_code()
    try:
        with get_db() as cur:
            cur.execute("INSERT INTO users (user_code) VALUES (%s) ON CONFLICT DO NOTHING", (user_code,))
        session['user_code'] = user_code
        session.permanent = True
        return jsonify({'success': True, 'user_code': user_code})
    except Exception as e:
        logger.error(f"Register error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/publish')
def publish():
    user_code = session.get('user_code')
    if not user_code:
        return redirect(url_for('index'))

    try:
        with get_db() as cur:
            cur.execute("SELECT * FROM channels WHERE user_code = %s", (user_code,))
            channels = cur.fetchall()
            cur.execute("SELECT is_premium FROM users WHERE user_code = %s", (user_code,))
            user = cur.fetchone()
            is_premium = user['is_premium'] if user else False

            posts = []
            for ch in channels:
                cur.execute("SELECT content, published_at FROM published_posts WHERE channel_id = %s ORDER BY published_at DESC LIMIT 3", (ch['channel_id'],))
                posts.extend(cur.fetchall())

            publish_time = get_publish_time()

            selected_channel_id = request.args.get('channel_id')
            selected_channel = None
            if selected_channel_id:
                for ch in channels:
                    if str(ch['id']) == selected_channel_id:
                        selected_channel = ch
                        break

            return render_template('publish.html', user_code=user_code, channels=channels,
                                   is_premium=is_premium, recent_posts=posts, publish_time=publish_time,
                                   selected_channel=selected_channel)
    except Exception as e:
        logger.error(f"Publish error: {e}")
        flash('حدث خطأ في تحميل البيانات', 'error')
        return render_template('publish.html', user_code=user_code, channels=[], is_premium=False,
                               recent_posts=[], publish_time='12:00', selected_channel=None)

@app.route('/publish/add_channel', methods=['POST'])
def add_channel():
    user_code = session.get('user_code')
    if not user_code:
        return jsonify({'success': False, 'message': 'يجب تسجيل الدخول أولاً'}), 401

    channel_id = request.form.get('channel_id', '').strip()
    channel_username = request.form.get('channel_username', '').strip()

    if not channel_id:
        return jsonify({'success': False, 'message': 'معرف القناة مطلوب'}), 400
    if not channel_username.startswith('@'):
        return jsonify({'success': False, 'message': 'اسم المستخدم يجب أن يبدأ بـ @'}), 400

    try:
        with get_db() as cur:
            cur.execute("SELECT COUNT(*) FROM channels WHERE user_code = %s", (user_code,))
            count = cur.fetchone()['count']
            cur.execute("SELECT is_premium FROM users WHERE user_code = %s", (user_code,))
            user = cur.fetchone()
            is_premium = user['is_premium'] if user else False

            if not is_premium and count >= 1:
                return jsonify({'success': False, 'message': 'أنت بحاجة إلى Premium لإضافة قناة ثانية. تواصل مع المالك @OlIiIl7'}), 403
            if is_premium and count >= 3:
                return jsonify({'success': False, 'message': 'لقد وصلت للحد الأقصى البالغ 3 قنوات'}), 403

            cur.execute("SELECT id FROM channels WHERE channel_id = %s", (channel_id,))
            if cur.fetchone():
                return jsonify({'success': False, 'message': 'هذه القناة مسجلة مسبقاً'}), 400

            cur.execute("INSERT INTO channels (channel_id, channel_username, user_code, is_active) VALUES (%s, %s, %s, true)",
                        (channel_id, channel_username, user_code))
            return jsonify({'success': True, 'message': 'تمت إضافة القناة بنجاح'})
    except Exception as e:
        logger.error(f"Add channel error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/publish/toggle_channel/<int:channel_id>')
def toggle_channel(channel_id):
    user_code = session.get('user_code')
    if not user_code:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    try:
        with get_db() as cur:
            cur.execute("SELECT is_active FROM channels WHERE id = %s AND user_code = %s", (channel_id, user_code))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'message': 'القناة غير موجودة'}), 404
            new_status = not row['is_active']
            cur.execute("UPDATE channels SET is_active = %s WHERE id = %s", (new_status, channel_id))
            return jsonify({'success': True, 'is_active': new_status})
    except Exception as e:
        logger.error(f"Toggle channel error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/publish/force_publish')
def force_publish():
    user_code = session.get('user_code')
    if not user_code:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    try:
        with get_db() as cur:
            cur.execute("SELECT channel_id FROM channels WHERE user_code = %s AND is_active = true", (user_code,))
            channels = cur.fetchall()
            if not channels:
                return jsonify({'success': False, 'message': 'لا توجد قنوات نشطة'}), 400
            cur.execute("SELECT id, prompt FROM content_templates ORDER BY RANDOM() LIMIT 1")
            template = cur.fetchone()
            content = generate_post_content(content_id=template['id']) if template else generate_post_content()
            if not content:
                return jsonify({'success': False, 'message': 'فشل توليد المحتوى'}), 500
            ch_id = channels[0]['channel_id']
            msg_id = send_telegram_message(ch_id, content)
            if msg_id:
                cur.execute("INSERT INTO published_posts (channel_id, content, message_id) VALUES (%s, %s, %s)",
                            (ch_id, content, msg_id))
                cur.execute("UPDATE channels SET last_post_at = NOW() WHERE channel_id = %s", (ch_id,))
                return jsonify({'success': True, 'message': 'تم النشر بنجاح'})
            else:
                return jsonify({'success': False, 'message': 'فشل النشر'}), 500
    except Exception as e:
        logger.error(f"Force publish error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ==================== Admin ====================
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
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    try:
        with get_db() as cur:
            cur.execute("SELECT * FROM users ORDER BY id")
            users = cur.fetchall()
            cur.execute("SELECT * FROM channels ORDER BY id")
            channels = cur.fetchall()
            cur.execute("SELECT * FROM content_templates ORDER BY id")
            templates = cur.fetchall()
            cur.execute("SELECT publish_times FROM publish_settings LIMIT 1")
            setting = cur.fetchone()
            publish_time = setting['publish_times'] if setting else '12:00'
            if publish_time and publish_time.startswith('['):
                try:
                    times = json.loads(publish_time)
                    if times and isinstance(times, list):
                        publish_time = times[0]
                except:
                    pass
            cur.execute("SELECT * FROM notifications ORDER BY id DESC")
            notifications = cur.fetchall()
        return render_template('admin.html', users=users, channels=channels,
                               templates=templates, publish_time=publish_time,
                               notifications=notifications)
    except Exception as e:
        logger.error(f"Admin panel error: {e}")
        flash('حدث خطأ في تحميل البيانات', 'error')
        return render_template('admin.html', users=[], channels=[], templates=[],
                               publish_time='12:00', notifications=[])

@app.route('/admin/user/toggle_premium/<string:user_code>')
def toggle_premium(user_code):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    try:
        with get_db() as cur:
            cur.execute("SELECT is_premium FROM users WHERE user_code = %s", (user_code,))
            row = cur.fetchone()
            if row:
                new_status = not row['is_premium']
                cur.execute("UPDATE users SET is_premium = %s WHERE user_code = %s", (new_status, user_code))
                flash(f'تم تحديث حالة Premium للمستخدم {user_code}', 'success')
            else:
                flash('المستخدم غير موجود', 'error')
    except Exception as e:
        logger.error(f"Toggle premium error: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/update_publish_time', methods=['POST'])
def update_publish_time():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    new_time = request.form.get('publish_time', '12:00')
    try:
        hour, minute = map(int, new_time.split(':'))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except:
        flash('تنسيق الوقت غير صحيح، استخدم HH:MM', 'error')
        return redirect(url_for('admin_panel'))
    try:
        with get_db() as cur:
            cur.execute("UPDATE publish_settings SET publish_times = %s", (new_time,))
        schedule_daily_job()
        flash('تم تحديث وقت النشر', 'success')
    except Exception as e:
        logger.error(f"Update publish time error: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/content/add', methods=['POST'])
def add_content():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    name = request.form.get('name')
    prompt = request.form.get('prompt')
    content = request.form.get('content')
    if not name or not content:
        flash('الاسم والمحتوى مطلوبان', 'error')
        return redirect(url_for('admin_panel'))
    try:
        with get_db() as cur:
            cur.execute("INSERT INTO content_templates (name, prompt, content) VALUES (%s, %s, %s)",
                        (name, prompt, content))
        flash('تمت إضافة المحتوى', 'success')
    except Exception as e:
        logger.error(f"Add content error: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/content/<int:id>/delete')
def delete_content(id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    try:
        with get_db() as cur:
            cur.execute("DELETE FROM content_templates WHERE id = %s", (id,))
        flash('تم حذف المحتوى', 'success')
    except Exception as e:
        logger.error(f"Delete content error: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/notification/add', methods=['POST'])
def add_notification():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    title = request.form.get('title')
    text = request.form.get('text')
    if not title or not text:
        flash('العنوان والنص مطلوبان', 'error')
        return redirect(url_for('admin_panel'))
    try:
        with get_db() as cur:
            cur.execute("INSERT INTO notifications (title, text, created_at) VALUES (%s, %s, NOW())", (title, text))
        flash('تم إرسال الإشعار', 'success')
    except Exception as e:
        logger.error(f"Add notification error: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/notification/<int:id>/delete')
def delete_notification(id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    try:
        with get_db() as cur:
            cur.execute("DELETE FROM notifications WHERE id = %s", (id,))
        flash('تم حذف الإشعار', 'success')
    except Exception as e:
        logger.error(f"Delete notification error: {e}")
        flash(str(e), 'error')
    return redirect(url_for('admin_panel'))

# ==================== API ====================
@app.route('/api/active_users')
def active_users():
    try:
        with get_db() as cur:
            cur.execute("SELECT COUNT(*) FROM users WHERE last_active > NOW() - INTERVAL '5 minutes'")
            count = cur.fetchone()['count']
        return jsonify({'count': count})
    except Exception as e:
        logger.error(f"Active users error: {e}")
        return jsonify({'count': 0})

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    message = data.get('message', '')
    prompt = "أنت مساعد ذكي للدردشة."
    headers = {'Authorization': f'Bearer {OPENROUTER_API_KEY}', 'Content-Type': 'application/json'}
    payload = {
        'model': 'openai/gpt-4o-mini',
        'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': message}],
        'temperature': 0.7
    }
    try:
        response = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=30)
        result = response.json()
        return jsonify({'response': result['choices'][0]['message']['content']})
    except Exception as e:
        logger.error(f"Chat error: {e}")
        return jsonify({'error': str(e)}), 500

# ==================== بدء التشغيل ====================
if __name__ == '__main__':
    init_db()
    schedule_daily_job()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False)
