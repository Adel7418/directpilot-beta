# P2 dependency admission

The P2 runtime additions are lockfile-pinned and kept to the PostgreSQL persistence path.

| Dependency | Locked version | Purpose | license | Admission and vulnerability control |
| --- | --- | --- | --- | --- |
| SQLAlchemy | 2.0.52 | SQLAlchemy 2 synchronous engine, sessions, and metadata | MIT | Required for the approved persistence seam. Lockfile review and the deterministic dependency audit must pass before release. |
| psycopg | 3.2.13 | PostgreSQL 17+ DBAPI driver for SQLAlchemy | LGPL-3.0-only | Required for PostgreSQL access; the binary extra is used only for the supported isolated runtime. Lockfile review and the deterministic dependency audit must pass before release. |
| Alembic | 1.17.2 | Deterministic schema versioning and upgrade checks | MIT | Required for migration ownership and compatibility checks. Lockfile review and the deterministic dependency audit must pass before release. |

The dependency audit result is evidence, not an authorization to change versions. Any vulnerability finding or license change requires security review before updating the lockfile. No dependency in this document handles provider credentials or causes live provider writes.
