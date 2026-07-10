# Flask webbdashboard för Jonatan
# Lösenordsskyddad, monitor, manage, interact med bot, logs/historik
from flask import Flask, render_template, request
app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')  # Monitor + logs

# TODO: /install, /manage, /interact, /logs (read-only för Jonatan)
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)