"""audits table — migrate audit history from outputs/*.json to SQL

Replaces the two-file-per-audit on-disk scheme at outputs/<site>_<type>_<ts>_{summary,full}.json
with a single SQL row per audit. See app.py Audit model for column
rationale.

Data migration: scans outputs/ for *_summary.json files and pairs each
with its sibling *_full.json. Rows are inserted into the audits table
with the filename (preserving URL stability) as the primary key. The
JSON files are left on disk — ops can rename outputs/ to outputs.bak/
after verifying the import.

Revision ID: d4e5f6a7b8c9
Revises: b2c3d4e5f6a7
Create Date: 2026-05-14 09:00:00.000000

"""
import json
import os

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c9'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


TABLE_NAME = "audits"
OUTPUTS_DIR = "outputs"


def upgrade():
    op.create_table(
        TABLE_NAME,
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('client_id', sa.String(length=255), nullable=True),
        sa.Column('client_name', sa.String(length=255), nullable=True),
        sa.Column('website', sa.String(length=500), nullable=True),
        sa.Column('audit_type', sa.String(length=40), nullable=True),
        sa.Column('saved_at', sa.String(length=40), nullable=False),
        sa.Column('normalized_score', sa.Float(), nullable=False, server_default='0'),
        sa.Column('visibility_score', sa.Float(), nullable=False, server_default='0'),
        sa.Column('content_score', sa.Float(), nullable=False, server_default='0'),
        sa.Column('schema_score', sa.Float(), nullable=False, server_default='0'),
        sa.Column('summary_payload', sa.JSON(), nullable=True),
        sa.Column('full_payload', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('filename'),
    )
    with op.batch_alter_table(TABLE_NAME, schema=None) as batch_op:
        batch_op.create_index('ix_audits_user_id', ['user_id'])
        batch_op.create_index('ix_audits_client_id', ['client_id'])
        batch_op.create_index('ix_audits_website', ['website'])
        batch_op.create_index('ix_audits_saved_at', ['saved_at'])

    _import_from_outputs()


def _safe_float(v, default=0.0):
    """Permissive float coercion — values in legacy JSON come in as
    int, float, str, or sometimes None depending on which save path
    wrote them. Defaults swallow the rest so the migration doesn't
    fail on one bad row."""
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _import_from_outputs():
    """Best-effort import of every *_summary.json file in outputs/
    into the new table. Skips files that can't be parsed; the schema
    migration still completes. Ops can re-run the import after
    cleaning the bad files."""
    if not os.path.isdir(OUTPUTS_DIR):
        return

    conn = op.get_bind()
    table = sa.table(
        TABLE_NAME,
        sa.column("filename", sa.String),
        sa.column("user_id", sa.Integer),
        sa.column("client_id", sa.String),
        sa.column("client_name", sa.String),
        sa.column("website", sa.String),
        sa.column("audit_type", sa.String),
        sa.column("saved_at", sa.String),
        sa.column("normalized_score", sa.Float),
        sa.column("visibility_score", sa.Float),
        sa.column("content_score", sa.Float),
        sa.column("schema_score", sa.Float),
        sa.column("summary_payload", sa.JSON),
        sa.column("full_payload", sa.JSON),
    )

    summary_files = sorted([
        f for f in os.listdir(OUTPUTS_DIR)
        if f.endswith("_summary.json")
    ])

    rows = []
    for summary_filename in summary_files:
        summary_path = os.path.join(OUTPUTS_DIR, summary_filename)

        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)
        except Exception:
            continue

        # Pair with the matching _full.json. Some legacy audits don't
        # have a full file (early CLI runs); they still get a row
        # with a None full_payload.
        full_filename = summary_filename.replace(
            "_summary.json", "_full.json"
        )
        full_path = os.path.join(OUTPUTS_DIR, full_filename)
        full_payload = None
        if os.path.exists(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    full_payload = json.load(f)
            except Exception:
                full_payload = None

        scores = summary.get("scores") or {}
        try:
            rows.append({
                "filename": summary_filename,
                "user_id": int(summary["user_id"]) if summary.get("user_id") not in (None, "") else None,
                "client_id": str(summary["client_id"]) if summary.get("client_id") not in (None, "") else None,
                "client_name": (summary.get("client_name") or "")[:255] or None,
                "website": (summary.get("website") or "")[:500] or None,
                "audit_type": (summary.get("audit_type") or "")[:40] or None,
                "saved_at": (summary.get("saved_at") or "")[:40] or _fallback_now(),
                "normalized_score": _safe_float(scores.get("normalized_score")),
                "visibility_score": _safe_float(scores.get("visibility_score")),
                "content_score": _safe_float(scores.get("content_score")),
                "schema_score": _safe_float(scores.get("schema_score")),
                "summary_payload": summary,
                "full_payload": full_payload,
            })
        except Exception:
            # Bad row shape — skip, don't fail the whole migration.
            continue

    # Batched insert if we have anything. Empty outputs/ is a no-op.
    if rows:
        conn.execute(table.insert(), rows)


def _fallback_now() -> str:
    """ISO-8601 UTC for audits missing a `saved_at` field. Defined
    locally so this migration runs without a Flask app context."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def downgrade():
    with op.batch_alter_table(TABLE_NAME, schema=None) as batch_op:
        batch_op.drop_index('ix_audits_saved_at')
        batch_op.drop_index('ix_audits_website')
        batch_op.drop_index('ix_audits_client_id')
        batch_op.drop_index('ix_audits_user_id')
    op.drop_table(TABLE_NAME)
