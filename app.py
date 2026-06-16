import os
import logging
import requests
import time
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'evile-secret-key-2026')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'evile2026')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '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')
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== بيانات وهمية في الذاكرة =====
characters = [
    {
        'id': 1,
        'name': 'مصمم شعارات',
        'description': 'متخصص في تصميم الشعارات الإبداعية',
        'prompt': 'أنت مصمم شعارات محترف. ساعد المستخدم في تصميم شعارات مبتكرة.',
        'callback_key': 'logo_maker',
        'logo_url': ''
    },
    {
        'id': 2,
        'name': 'كاتب محتوى',
        'description': 'كاتب إبداعي لمحتوى تسويقي',
        'prompt': 'أنت كاتب محتوى مبدع. اكتب نصوصاً تسويقية جذابة.',
        'callback_key': 'content_writer',
        'logo_url': ''
    },
    {
        'id': 3,
        'name': 'مستشار أعمال',
        'description': 'خبير في تطوير الأعمال والاستراتيجيات',
        'prompt': 'أنت مستشار أعمال محترف. قدم نصائح استراتيجية لتطوير الأعمال.',
        'callback_key': 'business_advisor',
        'logo_url': ''
    }
]

notifications = [
    {
        'id': 1,
        'title': 'ترحيب',
        'text': 'مرحباً بك في EVILE! استمتع بتجربتك.',
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'duration_hours': 1,
        'show_in_chat': True
    }
]

users = []

# ===== دوال مساعدة =====
def update_user_activity(telegram_id):
    pass

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ===== Routes =====
@app.route('/')
def index():
    telegram_id = session.get('telegram_id')
    latest_notification = None
    for n in reversed(notifications):
        if n.get('show_in_chat'):
            latest_notification = n
            break
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
        if telegram_id not in users:
            users.append(telegram_id)
        session['telegram_id'] = telegram_id
        session.permanent = True
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Register error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/active_users')
def api_active_users():
    return jsonify({'count': len(users)})

@app.route('/health')
def health_check():
    return jsonify({'status': 'healthy', 'database': 'mock', 'timestamp': datetime.now().isoformat()})

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
@admin_required
def admin_panel():
    return render_template('admin.html', characters=characters, notifications=notifications, users_count=len(users))

@app.route('/admin/character/add', methods=['POST'])
@admin_required
def add_character():
    name = request.form.get('name')
    description = request.form.get('description')
    prompt = request.form.get('prompt')
    callback_key = request.form.get('callback_key', name.lower().replace(' ', '_'))
    logo_url = request.form.get('logo_url', '')
    if name and description and prompt:
        new_id = max([c['id'] for c in characters], default=0) + 1
        characters.append({
            'id': new_id,
            'name': name,
            'description': description,
            'prompt': prompt,
            'callback_key': callback_key,
            'logo_url': logo_url
        })
        flash('تمت إضافة الشخصية بنجاح', 'success')
    else:
        flash('جميع الحقول مطلوبة', 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/character/<int:char_id>/edit', methods=['POST'])
@admin_required
def edit_character(char_id):
    name = request.form.get('name')
    description = request.form.get('description')
    prompt = request.form.get('prompt')
    logo_url = request.form.get('logo_url', '')
    for char in characters:
        if char['id'] == char_id:
            char['name'] = name
            char['description'] = description
            char['prompt'] = prompt
            char['logo_url'] = logo_url
            flash('تم تعديل الشخصية بنجاح', 'success')
            break
    else:
        flash('لم يتم العثور على الشخصية', 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/character/<int:char_id>/delete')
@admin_required
def delete_character(char_id):
    global characters
    characters = [c for c in characters if c['id'] != char_id]
    flash('تم حذف الشخصية', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/notification/add', methods=['POST'])
@admin_required
def add_notification():
    title = request.form.get('title')
    text = request.form.get('text')
    duration_hours = request.form.get('duration_hours', 1, type=int)
    show_in_chat = request.form.get('show_in_chat') == 'on'
    if title and text:
        new_id = max([n['id'] for n in notifications], default=0) + 1
        notifications.append({
            'id': new_id,
            'title': title,
            'text': text,
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'duration_hours': duration_hours,
            'show_in_chat': show_in_chat
        })
        flash('تم إرسال الإشعار بنجاح', 'success')
    else:
        flash('العنوان والنص مطلوبان', 'error')
    return redirect(url_for('admin_panel'))

@app.route('/api/notifications')
def api_notifications():
    return jsonify(notifications)

@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.json
    character_key = data.get('character', 'logo_maker')
    message = data.get('message', '')
    character = next((c for c in characters if c['callback_key'] == character_key), None)
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

@app.route('/publish')
def publish():
    telegram_id = session.get('telegram_id')
    latest_notification = None
    for n in reversed(notifications):
        if n.get('show_in_chat'):
            latest_notification = n
            break
    return render_template('publish.html',
                           characters=characters,
                           telegram_id=telegram_id,
                           latest_notification=latest_notification)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)
