virtualenv -p /usr/bin/python3 venv/
source venv/bin/activate
pip install -r vrs.txt
python setup.py install
uwsgi uwsgi.ini