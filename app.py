from flask import Flask, send_from_directory
import os

# تهيئة التطبيق. نحدد المجلد الحالي ليكون هو مجلد القوالب والملفات الثابتة
app = Flask(__name__, static_folder='.', template_folder='.')

@app.route('/')
def index():
    # عند زيارة الصفحة الرئيسية، يقوم الخادم بإرسال ملف index.html
    return send_from_directory('.', 'index.html')

# تشغيل الخادم
if __name__ == '__main__':
    # port=5000 هو المنفذ الافتراضي لفلاسك
    # debug=True يسمح بإعادة تشغيل الخادم تلقائياً عند حفظ التغييرات
    print("🚀 يتم تشغيل الموقع على http://127.0.0.1:5000")
    app.run(host='0.0.0.0', port=5000, debug=True)
