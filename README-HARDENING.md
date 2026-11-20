# Hardening patch (additive: 3 new files + 1 line)

Files: hardening.py, tests/test_hardening.py, gunicorn.conf.py

1. Copy the three files into the project (hardening.py next to app.py).
2. In app.py, inside create_app(), put these two lines directly above `return app`:
       import hardening
       hardening.apply(app)
3. Run:  python -m unittest tests.test_hardening     (8 tests, all must pass)
4. Local use is unchanged. Production environment variables:
       LR_PRODUCTION=1  LR_TRUSTED_PROXIES=1  LR_CSP=report   (then enforce, see below)
5. CSP: start in "report". Open the site, use every button, open the browser Console (Cmd+Option+J).
   No "Content Security Policy" messages = set LR_CSP=enforce. Any message = send it to me.
6. Start command on the host:  gunicorn -c gunicorn.conf.py wsgi:app
