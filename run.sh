export PORT=8080
virtualenv venv
. venv/bin/activate
uwsgi uwsgi.ini