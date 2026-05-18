from flask import Flask, render_template, request, redirect, url_for
import os
from parser import parse_sii

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        file = request.files.get('savefile')
        if file and file.filename.endswith('.sii'):
            path = os.path.join(UPLOAD_FOLDER, 'game-decoded.sii')
            file.save(path)
            return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    path = os.path.join(UPLOAD_FOLDER, 'game-decoded.sii')
    if not os.path.exists(path):
        return redirect(url_for('index'))
    data = parse_sii(path)
    return render_template('dashboard.html', data=data)

if __name__ == '__main__':
    app.run(debug=True)