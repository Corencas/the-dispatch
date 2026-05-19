"""
app.py — legacy entry point, kept for reference only.

The full server is in server/server.py. Run that instead:

    cd server && python server.py

"""
from flask import Flask, redirect

app = Flask(__name__)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    return redirect('http://localhost:5001/' + path, code=302)

if __name__ == '__main__':
    print('This is the legacy entry point. Run server/server.py instead.')
    print('  cd server && python server.py')
