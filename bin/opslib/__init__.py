"""Host-side operations tooling: staging deploys and ephemeral dev environments.

Deliberately stdlib-only and free of any import from ``src/urbanlens``. These
run on the host, often when the application is *not* running - a deploy tool
that needs the thing it is deploying is useless exactly when it is needed. The
same constraint the existing ``deploy_webhook.py`` was written under.
"""
