from flask import (
    Flask,
    render_template,
    abort,
    jsonify,
    request,
    redirect,
    url_for,
    flash,
    session,
)

from action_engine import build_recommended_actions

from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
import os
import json
from datetime import datetime

from audit_runner import run_audit_for_input
from content_brief_generator import generate_content_brief
from content_draft_generator import generate_content_draft
from help_content import HELP_GLOSSARY
from content_queue import (
    add_queue_item,
    get_queue_items,
    get_queue_item_by_id,
    update_queue_item_status,
    update_queue_item_content,
    get_client_progress,
    get_next_action,
    create_queue_item_from_audit_opportunity,
)

from flask_migrate import Migrate

app = Flask(__name__)
print("Flask app initialized")

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-to-a-random-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = "login"

OUTPUTS_FOLDER = "outputs"
DATA_FOLDER = "data"
CLIENTS_FILE = os.path.join(DATA_FOLDER, "clients.json")


# =========================
# Database models
# =========================

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    referral_code = db.Column(db.String(50), unique=True, nullable=True)
    referred_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    role = db.Column(db.String(50), default="user")
    plan = db.Column(db.String(50), default="free")
    is_white_label_enabled = db.Column(db.Boolean, default=False)
    agency_name = db.Column(db.String(255), nullable=True)

    wallet = db.relationship(
        "Wallet",
        backref="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    clients = db.relationship(
        "Client",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan",
    )


class Wallet(db.Model):
    __tablename__ = "wallets"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    balance = db.Column(db.Integer, default=0, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CreditTransaction(db.Model):
    __tablename__ = "credit_transactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    balance_after = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Referral(db.Model):
    __tablename__ = "referrals"

    id = db.Column(db.Integer, primary_key=True)
    referrer_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    referred_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    referral_code = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(50), default="pending", nullable=False)
    reward_amount_referrer = db.Column(db.Integer, default=0, nullable=False)
    reward_amount_referred = db.Column(db.Integer, default=0, nullable=False)
    qualified_at = db.Column(db.DateTime, nullable=True)
    rewarded_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(255), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    name = db.Column(db.String(255), nullable=False)
    website = db.Column(db.String(500), nullable=False)
    website_normalized = db.Column(db.String(500), nullable=False, index=True)

    industry = db.Column(db.String(255), nullable=True)
    location = db.Column(db.String(255), nullable=True)
    owner_type = db.Column(db.String(100), nullable=False, default="company")
    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "slug", name="uq_clients_user_slug"),
    )

class PromptTracking(db.Model):
    __tablename__ = "prompt_tracking"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    domain = db.Column(db.String(255), nullable=True, index=True)
    platform = db.Column(db.String(100), nullable=True, index=True)
    market = db.Column(db.String(255), nullable=True, index=True)
    topic = db.Column(db.String(255), nullable=True, index=True)

    prompt = db.Column(db.Text, nullable=False)

    status = db.Column(db.String(50), default="Tracking", nullable=False)
    visibility = db.Column(db.String(50), default="Low", nullable=False)
    mentioned = db.Column(db.String(50), default="No", nullable=False)
    top_competitor = db.Column(db.String(255), nullable=True)

    last_checked = db.Column(db.String(100), default="Just added", nullable=True)
    change = db.Column(db.String(50), default="New", nullable=True)

    prompt_score = db.Column(db.Integer, default=0, nullable=False)
    score_band = db.Column(db.String(50), default="Weak", nullable=True)
    opportunity_label = db.Column(db.String(100), default="High opportunity", nullable=True)
    brand_position = db.Column(db.String(100), default="Not mentioned", nullable=True)
    competitor_count = db.Column(db.Integer, default=0, nullable=False)
    source_support = db.Column(db.String(100), default="Low", nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# =========================
# General helpers
# =========================

def get_prompt_visibility(client_id, target_query):
    rows = PromptTracking.query.filter_by(
        user_id=current_user.id
    ).all()

    if not rows:
        return 30  # fallback

    total = len(rows)
    mentions = 0

    for row in rows:
        if row.domain and row.domain.lower() in (target_query or "").lower():
            mentions += 1

    visibility = (mentions / total) * 100
    return int(visibility)

def get_competitor_strength(client_id):
    rows = PromptTracking.query.filter_by(
        user_id=current_user.id
    ).all()

    if not rows:
        return 50

    competitor_hits = 0

    for row in rows:
        if row.top_competitor:
            competitor_hits += 1

    strength = (competitor_hits / len(rows)) * 100
    return int(strength)

def get_content_score(result):
    if not result:
        return 40

    text = str(result)

    length_score = min(len(text) / 1000 * 100, 100)

    return int(length_score)

def calculate_aeo_score(visibility=None, competitors=None, content_score=None):
    # fallback defaults
    visibility = visibility or 40
    competitors = competitors or 60
    content_score = content_score or 50

    # weighted scoring
    score = (
        (visibility * 0.4) +
        ((100 - competitors) * 0.3) +
        (content_score * 0.3)
    )

    # classify
    if score >= 70:
        opportunity = "High"
    elif score >= 40:
        opportunity = "Moderate"
    else:
        opportunity = "Low"

    return {
        "score": int(score),
        "opportunity": opportunity,
        "competitor_strength": "High" if competitors > 60 else "Low",
        "visibility": visibility
    }

def score_to_opportunity_label(score: float) -> str:
    if score >= 80:
        return "Strong visibility"
    if score >= 55:
        return "Moderate visibility"
    return "High opportunity"


def compute_prompt_visibility_score(
    brand_mentioned: bool,
    brand_position: int | None,
    competitor_count: int,
    source_support: str = "mixed",
) -> dict:
    # 1. Brand mention score (35)
    mention_score = 35 if brand_mentioned else 0

    # 2. Brand position score (25)
    if not brand_mentioned or brand_position is None:
        position_score = 0
    elif brand_position == 1:
        position_score = 25
    elif brand_position <= 3:
        position_score = 18
    elif brand_position <= 5:
        position_score = 10
    else:
        position_score = 4

    # 3. Competitor pressure score (20)
    if competitor_count == 0:
        competitor_score = 20
    elif competitor_count == 1:
        competitor_score = 14
    elif competitor_count == 2:
        competitor_score = 8
    else:
        competitor_score = 0

    # 4. Source support score (20)
    if source_support == "strong":
        source_score = 20
    elif source_support == "mixed":
        source_score = 10
    else:
        source_score = 0

    total = mention_score + position_score + competitor_score + source_score

    if total >= 80:
        band = "High"
    elif total >= 55:
        band = "Medium"
    else:
        band = "Low"

    return {
        "score": round(total, 1),
        "band": band,
        "mention_score": mention_score,
        "position_score": position_score,
        "competitor_score": competitor_score,
        "source_score": source_score,
        "opportunity_label": score_to_opportunity_label(total),
    }


def compute_mvp_prompt_inputs(
    visibility: str,
    mentioned: str,
    top_competitor: str | None,
) -> dict:
    brand_mentioned = mentioned in ["Yes", "Sometimes"]

    if mentioned == "Yes":
        brand_position = 1 if visibility == "High" else 3 if visibility == "Medium" else 5
    elif mentioned == "Sometimes":
        brand_position = 4
    else:
        brand_position = None

    competitor_count = 0 if not top_competitor or top_competitor == "—" else 1

    if visibility == "High" and mentioned == "Yes":
        source_support = "strong"
    elif mentioned in ["Yes", "Sometimes"]:
        source_support = "mixed"
    else:
        source_support = "weak"

    return {
        "brand_mentioned": brand_mentioned,
        "brand_position": brand_position,
        "competitor_count": competitor_count,
        "source_support": source_support,
    }


def apply_prompt_score(row: PromptTracking) -> None:
    inputs = compute_mvp_prompt_inputs(
        visibility=row.visibility,
        mentioned=row.mentioned,
        top_competitor=row.top_competitor,
    )

    result = compute_prompt_visibility_score(
        brand_mentioned=inputs["brand_mentioned"],
        brand_position=inputs["brand_position"],
        competitor_count=inputs["competitor_count"],
        source_support=inputs["source_support"],
    )

    row.brand_position = inputs["brand_position"]
    row.competitor_count = inputs["competitor_count"]
    row.source_support = inputs["source_support"]

    row.prompt_score = result["score"]
    row.score_band = result["band"]
    row.opportunity_label = result["opportunity_label"]

def ensure_data_dirs():
    os.makedirs(DATA_FOLDER, exist_ok=True)
    os.makedirs(OUTPUTS_FOLDER, exist_ok=True)


def load_json_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_load_json(filepath, default):
    if not os.path.exists(filepath):
        return default
    try:
        return load_json_file(filepath)
    except Exception:
        return default

def safe_str(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()

def normalize_website(url):
    if not url:
        return ""
    return (
        url.strip()
        .lower()
        .replace("https://", "")
        .replace("http://", "")
        .replace("www.", "")
        .rstrip("/")
    )


def slugify(text):
    if not text:
        return ""

    text = text.strip().lower()
    cleaned = []

    for ch in text:
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in [" ", "-", "_"]:
            cleaned.append("-")

    slug = "".join(cleaned)
    while "--" in slug:
        slug = slug.replace("--", "-")

    return slug.strip("-")


def generate_referral_code(name, user_id):
    base = slugify(name) or "user"
    return f"{base}-{user_id}"


# =========================
# Client helpers
# =========================

def serialize_client_row(client):
    return {
        "id": client.slug,
        "db_id": client.id,
        "user_id": client.user_id,
        "name": client.name,
        "website": client.website,
        "website_normalized": client.website_normalized,
        "industry": client.industry or "N/A",
        "location": client.location or "N/A",
        "owner_type": client.owner_type or "company",
        "notes": client.notes or "",
        "created_at": client.created_at.isoformat() if client.created_at else None,
        "updated_at": client.updated_at.isoformat() if client.updated_at else None,
    }


def get_unique_client_slug(user_id, name):
    base_slug = slugify(name) or "client"
    slug = base_slug
    counter = 2

    while Client.query.filter_by(user_id=user_id, slug=slug).first():
        slug = f"{base_slug}-{counter}"
        counter += 1

    return slug


def load_clients(user_id=None):
    query = Client.query
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    rows = query.order_by(Client.created_at.desc()).all()
    return [serialize_client_row(row) for row in rows]


def add_client(client_data, user_id):
    name = client_data.get("name", "").strip()
    website = client_data.get("website", "").strip()

    slug = get_unique_client_slug(user_id, name)

    row = Client(
        slug=slug,
        user_id=user_id,
        name=name,
        website=website,
        website_normalized=normalize_website(website),
        industry=client_data.get("industry", "").strip() or None,
        location=client_data.get("location", "").strip() or None,
        owner_type=client_data.get("owner_type", "company").strip() or "company",
        notes=client_data.get("notes", "").strip() or None,
    )

    db.session.add(row)
    db.session.commit()
    return serialize_client_row(row)


def get_client_row_by_slug(client_slug, user_id):
    return Client.query.filter_by(slug=client_slug, user_id=user_id).first()


def update_client(client_slug, user_id, client_data):
    row = get_client_row_by_slug(client_slug, user_id)
    if not row:
        return None

    new_name = client_data.get("name", "").strip()
    new_website = client_data.get("website", "").strip()

    if not new_name or not new_website:
        return None

    if new_name != row.name:
        desired_slug = slugify(new_name) or "client"
        if desired_slug != row.slug:
            unique_slug = desired_slug
            counter = 2
            while Client.query.filter(
                Client.user_id == user_id,
                Client.slug == unique_slug,
                Client.id != row.id,
            ).first():
                unique_slug = f"{desired_slug}-{counter}"
                counter += 1
            row.slug = unique_slug

    row.name = new_name
    row.website = new_website
    row.website_normalized = normalize_website(new_website)
    row.industry = client_data.get("industry", "").strip() or None
    row.location = client_data.get("location", "").strip() or None
    row.owner_type = client_data.get("owner_type", "company").strip() or "company"
    row.notes = client_data.get("notes", "").strip() or None

    db.session.commit()
    return serialize_client_row(row)


def delete_client_and_related_queue(client_slug, user_id):
    row = get_client_row_by_slug(client_slug, user_id)
    if not row:
        return False

    db.session.delete(row)
    db.session.commit()
    return True


# =========================
# Audit helpers
# =========================

def get_matching_full_filename(summary_filename):
    if not summary_filename.endswith("_summary.json"):
        return None
    return summary_filename.replace("_summary.json", "_full.json")


def get_summary_path(summary_filename):
    if not summary_filename.endswith("_summary.json"):
        return None
    summary_path = os.path.join(OUTPUTS_FOLDER, summary_filename)
    if not os.path.exists(summary_path):
        return None
    return summary_path


def get_full_path(summary_filename):
    full_filename = get_matching_full_filename(summary_filename)
    if not full_filename:
        return None
    full_path = os.path.join(OUTPUTS_FOLDER, full_filename)
    if not os.path.exists(full_path):
        return None
    return full_path


def read_full_audit_data(summary_filename):
    full_path = get_full_path(summary_filename)
    if not full_path:
        return None
    return safe_load_json(full_path, None)


def get_saved_audits(user_id=None):
    if not os.path.exists(OUTPUTS_FOLDER):
        return []

    files = os.listdir(OUTPUTS_FOLDER)
    summary_files = sorted([f for f in files if f.endswith("_summary.json")], reverse=True)

    audits = []
    for filename in summary_files:
        filepath = os.path.join(OUTPUTS_FOLDER, filename)

        try:
            data = load_json_file(filepath)

            saved_user_id = data.get("user_id")
            if user_id is not None and str(saved_user_id) != str(user_id):
                continue

            website = data.get("website", "N/A")
            audits.append({
                "filename": filename,
                "website": website,
                "website_normalized": normalize_website(website),
                "client_id": str(data.get("client_id")) if data.get("client_id") is not None else None,
                "client_name": data.get("client_name"),
                "audit_type": data.get("audit_type", "N/A"),
                "saved_at": data.get("saved_at", ""),
                "verdict": data.get("summary", {}).get("verdict", "N/A"),
                "opportunity_level": data.get("summary", {}).get("opportunity_level", "N/A"),
                "normalized_score": data.get("scores", {}).get("normalized_score", 0),
                "visibility_score": data.get("scores", {}).get("visibility_score", 0),
                "content_score": data.get("scores", {}).get("content_score", 0),
                "schema_score": data.get("scores", {}).get("schema_score", 0),
                "scores": data.get("scores", {}),
                "summary": data.get("summary", {}),
                "visibility_snapshot": data.get("visibility_snapshot", {}),
                "top_competitors": data.get("top_competitors", []),
                "top_content_gaps": data.get("top_content_gaps", []),
                "top_recommendations": data.get("top_recommendations", []),
            })
        except Exception as e:
            audits.append({
                "filename": filename,
                "website": "Error reading file",
                "website_normalized": "",
                "client_id": None,
                "client_name": None,
                "audit_type": "N/A",
                "saved_at": "",
                "verdict": str(e),
                "opportunity_level": "N/A",
                "normalized_score": 0,
                "visibility_score": 0,
                "content_score": 0,
                "schema_score": 0,
                "scores": {},
                "summary": {},
                "visibility_snapshot": {},
                "top_competitors": [],
                "top_content_gaps": [],
                "top_recommendations": [],
            })

    return audits

def filter_audits(audits, search_term="", audit_type="all"):
    results = audits

    if search_term:
        q = search_term.strip().lower()
        results = [
            audit for audit in results
            if q in audit.get("website", "").lower()
            or q in audit.get("verdict", "").lower()
            or q in audit.get("opportunity_level", "").lower()
            or q in (audit.get("client_name") or "").lower()
        ]

    if audit_type and audit_type != "all":
        results = [audit for audit in results if audit.get("audit_type", "").lower() == audit_type.lower()]

    return results


def sort_audits(audits, sort_by="saved_at", order="desc"):
    reverse = order == "desc"

    def safe_value(audit):
        if sort_by == "website":
            return audit.get("website", "").lower()
        if sort_by == "normalized_score":
            return audit.get("normalized_score", 0)
        if sort_by == "visibility_score":
            return audit.get("visibility_score", 0)
        if sort_by == "audit_type":
            return audit.get("audit_type", "").lower()
        return audit.get("saved_at", "")

    return sorted(audits, key=safe_value, reverse=reverse)


# =========================
# Comparison + actions
# =========================

def compare_audits(latest_audit, previous_audit):
    if not latest_audit or not previous_audit:
        return None

    def delta(current, previous):
        return round((current or 0) - (previous or 0), 2)

    normalized_delta = delta(latest_audit.get("normalized_score", 0), previous_audit.get("normalized_score", 0))
    visibility_delta = delta(latest_audit.get("visibility_score", 0), previous_audit.get("visibility_score", 0))
    content_delta = delta(latest_audit.get("content_score", 0), previous_audit.get("content_score", 0))
    schema_delta = delta(latest_audit.get("schema_score", 0), previous_audit.get("schema_score", 0))

    if normalized_delta > 0:
        overall_change = "improved"
    elif normalized_delta < 0:
        overall_change = "declined"
    else:
        overall_change = "unchanged"

    return {
        "latest": latest_audit,
        "previous": previous_audit,
        "normalized_delta": normalized_delta,
        "visibility_delta": visibility_delta,
        "content_delta": content_delta,
        "schema_delta": schema_delta,
        "overall_change": overall_change,
        "verdict_changed": latest_audit.get("verdict") != previous_audit.get("verdict"),
    }


def build_query_level_comparison(latest_summary_audit, previous_summary_audit):
    empty_response = {
        "rows": [],
        "summary": {
            "total_queries": 0,
            "improved": 0,
            "declined": 0,
            "changed": 0,
            "unchanged": 0,
            "missed_brand_mentions": 0,
        },
    }

    if not latest_summary_audit or not previous_summary_audit:
        return empty_response

    latest_full = read_full_audit_data(latest_summary_audit.get("filename"))
    previous_full = read_full_audit_data(previous_summary_audit.get("filename"))

    if not latest_full or not previous_full:
        return empty_response

    latest_rows = latest_full.get("ai_answer_results", [])
    previous_rows = previous_full.get("ai_answer_results", [])

    latest_map = {row.get("query", ""): row for row in latest_rows if row.get("query")}
    previous_map = {row.get("query", ""): row for row in previous_rows if row.get("query")}

    all_queries = sorted(set(latest_map.keys()) | set(previous_map.keys()))
    comparisons = []

    improved = declined = changed = unchanged = missed_brand_mentions = 0

    for query in all_queries:
        latest_row = latest_map.get(query, {})
        previous_row = previous_map.get(query, {})

        latest_score = latest_row.get("score", 0)
        previous_score = previous_row.get("score", 0)
        score_delta = round(latest_score - previous_score, 2)

        latest_brand = latest_row.get("brand_mentioned", False)
        previous_brand = previous_row.get("brand_mentioned", False)

        latest_position = latest_row.get("brand_position")
        previous_position = previous_row.get("brand_position")

        if score_delta > 0:
            change_type = "improved"
            improved += 1
        elif score_delta < 0:
            change_type = "declined"
            declined += 1
        else:
            if latest_brand != previous_brand or latest_position != previous_position:
                change_type = "changed"
                changed += 1
            else:
                change_type = "unchanged"
                unchanged += 1

        if not latest_brand:
            missed_brand_mentions += 1

        comparisons.append({
            "query": query,
            "latest_brand_mentioned": latest_brand,
            "previous_brand_mentioned": previous_brand,
            "latest_brand_position": latest_position,
            "previous_brand_position": previous_position,
            "latest_score": latest_score,
            "previous_score": previous_score,
            "score_delta": score_delta,
            "change_type": change_type,
            "latest_competitors": latest_row.get("latest_competitors", latest_row.get("competitors_mentioned", [])),
            "previous_competitors": previous_row.get("previous_competitors", previous_row.get("competitors_mentioned", [])),
        })

    return {
        "rows": comparisons,
        "summary": {
            "total_queries": len(all_queries),
            "improved": improved,
            "declined": declined,
            "changed": changed,
            "unchanged": unchanged,
            "missed_brand_mentions": missed_brand_mentions,
        },
    }

def build_client_views():
    clients = load_clients(user_id=current_user.id)
    audits = get_saved_audits(user_id=current_user.id)

    client_views = []

    for client in clients:
        client_id_str = str(client.get("id")) if client.get("id") is not None else ""
        client_website_norm = client.get("website_normalized") or ""

        matched_audits = [
            audit for audit in audits
            if (
                (audit.get("client_id") and str(audit.get("client_id")) == client_id_str)
                or (
                    not audit.get("client_id")
                    and audit.get("website_normalized") == client_website_norm
                )
            )
        ]

        matched_audits = sort_audits(matched_audits, sort_by="saved_at", order="desc")
        latest_audit = matched_audits[0] if matched_audits else None
        previous_audit = matched_audits[1] if len(matched_audits) > 1 else None
        comparison = compare_audits(latest_audit, previous_audit)
        query_comparison = build_query_level_comparison(latest_audit, previous_audit)
        query_rows = query_comparison.get("rows", []) if query_comparison else []

        recommended_actions = build_recommended_actions(
            client_name=client.get("name", ""),
            website=client.get("website", ""),
            scores=latest_audit.get("scores", {}) if latest_audit else {},
            query_analysis=[
                {
                    "query": row.get("query"),
                    "brand_mentioned": row.get("latest_brand_mentioned", False),
                    "score": row.get("latest_score", 0),
                    "score_delta": row.get("score_delta", 0),
                    "competitors_mentioned": row.get("latest_competitors", []),
                }
                for row in query_rows
            ],
            competitor_analysis={
                "top_competitors": [
                    {"name": c, "mention_count": 1}
                    for row in query_rows
                    for c in (row.get("latest_competitors", []) or [])
                ]
            },
            site_findings={},
        )
        client_views.append({
            **client,
            "audit_count": len(matched_audits),
            "latest_audit": latest_audit,
            "previous_audit": previous_audit,
            "comparison": comparison,
            "query_comparison": query_comparison,
            "recommended_actions": recommended_actions,
            "audits": matched_audits,
            "benchmark_items": [],
            "market_voice": None,
        })
    return client_views

def get_client_by_id(client_id):
    for client in build_client_views():
        if client.get("id") == client_id:
            return client
    return None


# =========================
# Credits
# =========================

def refund_credits(user, amount, tx_type="refund", notes=""):
    if user_has_unlimited_credits(user):
        wallet = user.wallet
        balance_after = wallet.balance if wallet else 0
        tx = CreditTransaction(
            user_id=user.id,
            type=f"{tx_type}_bypass",
            amount=0,
            balance_after=balance_after,
            notes=notes or "No refund needed for unlimited dev/admin user",
        )
        db.session.add(tx)
        db.session.commit()
        return True

    wallet = user.wallet
    if not wallet:
        return False

    wallet.balance += amount
    tx = CreditTransaction(
        user_id=user.id,
        type=tx_type,
        amount=amount,
        balance_after=wallet.balance,
        notes=notes,
    )
    db.session.add(tx)
    db.session.commit()
    return True

def user_has_unlimited_credits(user):
    if not user:
        return False

    if user.email == "pypteltd@gmail.com":
        return True

    return user.role == "admin" or user.plan == "dev_unlimited"

def get_view_mode(user):
    forced_mode = session.get("dev_view_mode")
    if forced_mode in ["single", "multi", "admin"]:
        return forced_mode

    if not user:
        return "single"

    if user.role == "admin" or user.plan == "dev_unlimited":
        return "admin"

    if user.plan in ["starter", "pro", "growth", "agency"]:
        return "multi"

    return "single"

@app.route("/dev/view-mode/<mode>")
@login_required
def dev_set_view_mode(mode):
    if current_user.email != "pypteltd@gmail.com" and current_user.role != "admin":
        abort(403)

    if mode not in ["single", "multi", "admin", "auto"]:
        abort(404)

    if mode == "auto":
        session.pop("dev_view_mode", None)
    else:
        session["dev_view_mode"] = mode

    return redirect(request.referrer or url_for("index"))

def require_internal_access():
    if not current_user.is_authenticated:
        abort(403)

    if current_user.role == "admin" or current_user.plan == "dev_unlimited" or current_user.email == "pypteltd@gmail.com":
        return

    abort(403)

def spend_credits(user, amount, tx_type="usage", notes=""):
    wallet = user.wallet

    if user_has_unlimited_credits(user):
        balance_after = wallet.balance if wallet else 0
        tx = CreditTransaction(
            user_id=user.id,
            type=f"{tx_type}_bypass",
            amount=0,
            balance_after=balance_after,
            notes=notes or "Unlimited dev/admin usage",
        )
        db.session.add(tx)
        db.session.commit()
        return True

    if not wallet or wallet.balance < amount:
        return False

    wallet.balance -= amount
    tx = CreditTransaction(
        user_id=user.id,
        type=tx_type,
        amount=-amount,
        balance_after=wallet.balance,
        notes=notes,
    )
    db.session.add(tx)
    db.session.commit()
    return True

def has_enough_credits(user, amount):
    if user_has_unlimited_credits(user):
        return True

    if not user or not user.wallet:
        return False

    return user.wallet.balance >= amount

def award_referral_if_qualified(user):
    referral = Referral.query.filter_by(referred_user_id=user.id, status="pending").first()
    if not referral:
        return False

    referrer = User.query.get(referral.referrer_user_id)
    referred_user = User.query.get(referral.referred_user_id)

    if not referrer or not referrer.wallet or not referred_user or not referred_user.wallet:
        return False

    referrer.wallet.balance += referral.reward_amount_referrer
    db.session.add(CreditTransaction(
        user_id=referrer.id,
        type="referral_bonus",
        amount=referral.reward_amount_referrer,
        balance_after=referrer.wallet.balance,
        notes=f"Referral reward for user {referred_user.email}",
    ))

    referred_user.wallet.balance += referral.reward_amount_referred
    db.session.add(CreditTransaction(
        user_id=referred_user.id,
        type="referral_bonus",
        amount=referral.reward_amount_referred,
        balance_after=referred_user.wallet.balance,
        notes="Referral bonus after qualification",
    ))

    referral.status = "rewarded"
    referral.qualified_at = datetime.utcnow()
    referral.rewarded_at = datetime.utcnow()

    db.session.commit()
    return True


def get_focused_client_for_user(user):
    clients = build_client_views()

    if not clients:
        return None

    explicit_default = next((c for c in clients if c.get("is_default")), None)
    if explicit_default:
        return explicit_default

    def sort_key(client):
        return client.get("updated_at") or ""

    clients_sorted = sorted(clients, key=sort_key, reverse=True)
    return clients_sorted[0]

# =========================
# Routes
# =========================



@app.route("/help")
@login_required
def help_page():
    return render_template("help.html", glossary=HELP_GLOSSARY)

@app.route("/dashboard")
@login_required
def dashboard():
    return redirect(url_for("index"))

@app.route("/pricing")
@login_required
def pricing_page():
    return render_template("pricing.html")

@app.route("/")
def index():
    if current_user.is_authenticated:
        all_audits = get_saved_audits(user_id=current_user.id)
        clients = build_client_views()

        search_term = request.args.get("q", "").strip()
        audit_type = request.args.get("type", "all").strip().lower()
        sort_by = request.args.get("sort", "saved_at").strip()
        order = request.args.get("order", "desc").strip().lower()

        audits = filter_audits(all_audits, search_term=search_term, audit_type=audit_type)
        audits = sort_audits(audits, sort_by=sort_by, order=order)

        # ✅ FIX: use real audit scores
        scores = [
            c.get("latest_audit", {}).get("normalized_score")
            for c in clients
            if c.get("latest_audit")
        ]

        overall_score = round(sum(scores) / len(scores), 1) if scores else 0

        return render_template(
            "dashboard.html",
            audits=audits,
            clients=clients,
            overall_score=overall_score,  # ✅ NEW
            total_audits=len(all_audits),
            search_term=search_term,
            selected_type=audit_type,
            selected_sort=sort_by,
            selected_order=order,
        )

    return redirect(url_for("login"))


@app.route("/dev/set-plan/<plan>")
@login_required
def dev_set_plan(plan):
    if current_user.email != "pypteltd@gmail.com" and current_user.role != "admin":
        abort(403)

    allowed_plans = ["free", "starter", "pro", "growth", "agency", "dev_unlimited"]
    if plan not in allowed_plans:
        abort(404)

    current_user.plan = plan
    db.session.commit()

    flash(f"Plan changed to {plan}.", "success")
    return redirect(request.referrer or url_for("index"))

from flask import make_response

@app.route("/client/<client_id>/export-pdf")
@login_required
def export_client_audit_pdf(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    html = render_template("client_audit_pdf.html", client=client)
    response = make_response(html)
    response.headers["Content-Type"] = "text/html"
    response.headers["Content-Disposition"] = f'inline; filename="{client_id}-audit-report.html"'
    return response

@app.route("/clients")
@login_required
def clients_page():
    view_mode = get_view_mode(current_user)
    focused_client = get_focused_client_for_user(current_user)

    if view_mode == "single" and focused_client:
        return redirect(url_for("client_detail", client_id=focused_client["id"]))

    return render_template(
        "clients.html",
        clients=build_client_views(),
        view_mode=view_mode,
        focused_client=focused_client,
    )

@app.route("/client/<client_id>/report")
@login_required
def report_page(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)
    return render_template("report_page.html", client=client)

def get_workspace_limit(user):
    if not user:
        return 0

    if user.role == "admin" or user.plan == "dev_unlimited":
        return None

    limits = {
        "free": 1,
        "starter": 3,
        "pro": 10,
        "growth": 10,
        "agency": 25,
    }

    return limits.get(user.plan, 1)

def get_workspace_count(user_id):
    return Client.query.filter_by(user_id=user_id).count()


def can_create_workspace(user):
    limit = get_workspace_limit(user)
    count = get_workspace_count(user.id)

    if limit is None:
        return True, None, count

    return count < limit, limit, count

@app.route("/create-checkout-session")
@login_required
def create_checkout_session():
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",  # simple one-time payment
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "Insights OS Pro Upgrade",
                    },
                    "unit_amount": 2900,  # $29.00
                },
                "quantity": 1,
            }],
            success_url=url_for("payment_success", _external=True),
            cancel_url=url_for("pricing_page", _external=True),
        )

        return redirect(session.url, code=303)

    except Exception as e:
        return str(e)

@app.route("/payment-success")
@login_required
def payment_success():
    current_user.plan = "pro"
    db.session.commit()

    flash("Upgrade successful! You now have full access 🚀", "success")
    return redirect(url_for("dashboard"))

@app.route("/clients/new", methods=["GET", "POST"])
@login_required
def create_client():
    view_mode = get_view_mode(current_user)
    existing_clients = build_client_views()

    # Single mode = one business only
    if view_mode == "single" and len(existing_clients) >= 1:
        flash(
            "Your current plan supports 1 workspace only. Upgrade to add more workspaces.",
            "warning",
        )
        return redirect(url_for("pricing_page"))

    allowed, limit, count = can_create_workspace(current_user)

    if not allowed:
        flash(
            f"You’ve reached your workspace limit ({count}/{limit}) for your current plan. Upgrade to add more workspaces.",
            "warning",
        )
        return redirect(url_for("pricing_page"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        website = request.form.get("website", "").strip()
        industry = request.form.get("industry", "").strip()
        location = request.form.get("location", "").strip()
        owner_type = request.form.get("owner_type", "company").strip()
        notes = request.form.get("notes", "").strip()

        if not name or not website:
            return render_template(
                "client_form.html",
                error="Client name and website are required.",
                form_data=request.form,
                mode="create",
                client=None,
            )

        client = add_client(
            {
                "name": name,
                "website": website,
                "industry": industry,
                "location": location,
                "owner_type": owner_type,
                "notes": notes,
            },
            user_id=current_user.id,
        )

        flash("Client workspace created successfully.", "success")
        return redirect(url_for("client_detail", client_id=client["id"]))

    return render_template(
        "client_form.html",
        error=None,
        form_data={},
        mode="create",
        client=None,
    )

@app.route("/client/<client_id>/edit", methods=["GET", "POST"])
@login_required
def edit_client(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        website = request.form.get("website", "").strip()
        industry = request.form.get("industry", "").strip()
        location = request.form.get("location", "").strip()
        owner_type = request.form.get("owner_type", "company").strip()
        notes = request.form.get("notes", "").strip()

        if not name or not website:
            return render_template(
                "client_form.html",
                error="Client name and website are required.",
                form_data=request.form,
                mode="edit",
                client=client,
            )

        updated_client = update_client(client_id, current_user.id, {
            "name": name,
            "website": website,
            "industry": industry,
            "location": location,
            "owner_type": owner_type,
            "notes": notes,
        })

        if not updated_client:
            return render_template(
                "client_form.html",
                error="Unable to update client.",
                form_data=request.form,
                mode="edit",
                client=client,
            )

        flash("Client updated successfully.")
        return redirect(url_for("client_detail", client_id=updated_client["id"]))

    return render_template(
        "client_form.html",
        error=None,
        form_data=client,
        mode="edit",
        client=client,
    )


@app.route("/client/<client_id>/delete", methods=["POST"])
@login_required
def delete_client(client_id):
    deleted = delete_client_and_related_queue(client_id, current_user.id)
    if not deleted:
        abort(404)

    flash("Client deleted successfully.")
    return redirect(url_for("clients_page"))


@app.route("/client/<client_id>")
@login_required
def client_detail(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)
    return render_template("client_detail.html", client=client)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        referral_code = request.form.get("referral_code", "").strip()

        if not name or not email or not password:
            return render_template("signup.html", error="All fields are required.")

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            return render_template("signup.html", error="Email already registered.")

        referrer = None
        if referral_code:
            referrer = User.query.filter_by(referral_code=referral_code).first()

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            referred_by_user_id=referrer.id if referrer else None,
        )
        db.session.add(user)
        db.session.flush()

        user.referral_code = generate_referral_code(name, user.id)

        wallet = Wallet(user_id=user.id, balance=3)
        db.session.add(wallet)

        tx = CreditTransaction(
            user_id=user.id,
            type="signup_bonus",
            amount=3,
            balance_after=3,
            notes="Starter credits on signup",
        )
        db.session.add(tx)

        if referrer and referrer.id != user.id:
            referral = Referral(
                referrer_user_id=referrer.id,
                referred_user_id=user.id,
                referral_code=referral_code,
                status="pending",
                reward_amount_referrer=2,
                reward_amount_referred=1,
            )
            db.session.add(referral)

        db.session.commit()

        login_user(user)
        flash("Account created successfully. You received 3 starter credits.")
        return redirect(url_for("index"))

    return render_template("signup.html", error=None)


from flask import session

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        user = User.query.filter_by(email=email).first()

        if not user or not check_password_hash(user.password_hash, password):
            return render_template("login.html", error="Invalid email or password.")

        session.pop("_flashes", None)
        login_user(user)
        flash("Logged in successfully.", "success")
        return redirect(url_for("index"))

    return render_template("login.html", error=None)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("aeo_agency_page"))


@app.route("/client/<client_id>/run-audit", methods=["GET", "POST"])
@login_required
def run_client_audit(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    if request.method == "POST":
        website = request.form.get("website")
        industry = request.form.get("industry")
        location = request.form.get("location")
        topic = request.form.get("topic")
        audit_type = request.form.get("audit_type", "quick")

        if not has_enough_credits(current_user, 1):
            flash("You don’t have enough credits to run another audit.", "warning")
            return redirect(url_for("pricing_page"))

        if not spend_credits(current_user, 1, notes="Client audit run"):
            flash("Unable to deduct credits for audit.", "warning")
            return redirect(url_for("pricing_page"))

        try:
            run_audit_for_input(
                website=website,
                industry=industry,
                location=location,
                topic=topic,
                audit_type=audit_type,
                client_id=client_id,
                user_id=current_user.id,
            )
            flash("Audit completed successfully.", "success")
            return redirect(url_for("client_detail", client_id=client_id))

        except Exception as e:
            refund_credits(current_user, 1, notes="Refund for failed client audit")
            flash(f"Audit failed: {str(e)}", "error")
            return redirect(url_for("client_detail", client_id=client_id))
                
    return render_template("new_audit.html", client=client)

@app.route("/generate-content/<int:prompt_id>")
@login_required
def generate_content_from_prompt(prompt_id):
    row = PromptTracking.query.filter_by(
        id=prompt_id,
        user_id=current_user.id
    ).first()

    if not row:
        flash("Prompt not found.", "warning")
        return redirect(url_for("prompt_management_page"))

    return redirect(url_for(
        "content_queue_page",
        query=row.prompt,
        topic=row.topic,
        source="prompt_tracking"
    ))

@app.route("/client/<client_id>/content-brief", methods=["GET", "POST"])
@login_required
def generate_client_content_brief(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    default_query = ""
    if client.get("query_comparison", {}).get("rows"):
        default_query = client["query_comparison"]["rows"][0].get("query", "")

    if request.method == "POST":
        target_query = safe_str(request.form.get("target_query"))
        content_type = safe_str(request.form.get("content_type", "service_page")) or "service_page"
        brand_context = safe_str(request.form.get("brand_context"))

        if not target_query:
            return render_template(
                "content_brief_form.html",
                client=client,
                error="Target query is required.",
                form_data=request.form
            )

        if not has_enough_credits(current_user, 1):
            flash("You don’t have enough credits to generate another brief.", "warning")
            return redirect(url_for("pricing_page"))

        if not spend_credits(current_user, 1, notes="Content brief generation"):
            flash("Unable to deduct credits for brief generation.", "warning")
            return redirect(url_for("pricing_page"))

        try:
            result = generate_content_brief(
                client_name=client.get("name", ""),
                website=client.get("website", ""),
                industry=client.get("industry", ""),
                location=client.get("location", ""),
                target_query=target_query,
                content_type=content_type,
                brand_context=brand_context,
            )

            flash("Content brief generated successfully.")

            tracked_rows = PromptTracking.query.filter_by(
                user_id=current_user.id
            ).filter(
                PromptTracking.prompt.ilike(f"%{target_query}%")
            ).all()

            top_competitors = list(set([
                row.top_competitor
                for row in tracked_rows
                if row.top_competitor and row.top_competitor != "—"
            ]))[:3]

            if tracked_rows:
                total_rows = len(tracked_rows)

                visible_rows = [
                    row for row in tracked_rows
                    if (row.mentioned or "").strip() in ["Yes", "Sometimes"]
                ]
                visibility = int((len(visible_rows) / total_rows) * 100) if total_rows > 0 else 30

                competitor_rows = [
                    row for row in tracked_rows
                    if row.top_competitor and row.top_competitor != "—"
                ]
                competitors = int((len(competitor_rows) / total_rows) * 100) if total_rows > 0 else 50
            else:
                visibility = 30
                competitors = 50

            brief_text = result.get("brief", "") if isinstance(result, dict) else str(result)
            content_score = min(max(int(len(brief_text) / 12), 20), 100)

            aeo = calculate_aeo_score(
                visibility=visibility,
                competitors=competitors,
                content_score=content_score,
            )

            return render_template(
                "content_brief_result.html",
                result=result,
                client=client,
                aeo=aeo,
                top_competitors=top_competitors,
                tracked_prompt_count=len(tracked_rows),
            )

        except Exception as e:
            refund_credits(current_user, 1, notes="Refund for failed content brief generation")
            return render_template(
                "content_brief_form.html",
                client=client,
                error=f"Brief generation failed: {str(e)}",
                form_data=request.form
            )

    prefill_query = safe_str(request.args.get("target_query"))
    prefill_context = safe_str(request.args.get("brand_context"))

    form_data = {
        "target_query": prefill_query if prefill_query else default_query,
        "content_type": "service_page",
        "brand_context": prefill_context if prefill_context else client.get("notes", ""),
    }

    return render_template(
        "content_brief_form.html",
        client=client,
        error=None,
        form_data=form_data
    )

@app.route("/generate-brief/<item_id>")
@login_required
def generate_brief_from_queue(item_id):
    item = get_queue_item_by_id(item_id, user_id=current_user.id)

    if not item:
        flash("Queue item not found.", "error")
        return redirect(url_for("content_queue_page"))

    if not has_enough_credits(current_user, 1):
        flash("You don’t have enough credits to generate another brief.", "warning")
        return redirect(url_for("pricing_page"))

    if not spend_credits(current_user, 1, notes="Queue content brief generation"):
        flash("Unable to deduct credits for brief generation.", "warning")
        return redirect(url_for("pricing_page"))

    try:
        brief = generate_content_brief(
            client_name=item.get("client_name", ""),
            website=item.get("website", ""),
            industry=item.get("industry", ""),
            location=item.get("location", ""),
            target_query=item.get("target_query", ""),
            content_type=item.get("content_type", "service_page"),
            brand_context=item.get("brand_context", ""),
        )

        updated_item = update_queue_item_content(
            item_id,
            content=brief,
            status="brief_generated",
            user_id=current_user.id,
        )

        if not updated_item:
            refund_credits(current_user, 1, notes="Refund for unsaved queue brief")
            flash("Brief was generated but could not be saved to the queue item.", "error")
            return redirect(url_for("content_queue_page", client_id=item.get("client_id")))

        flash("Brief generated successfully.", "success")
        return redirect(url_for("content_queue_page", client_id=item.get("client_id")))

    except Exception as e:
        refund_credits(current_user, 1, notes="Refund for failed queue brief generation")
        flash(f"Failed to generate brief: {str(e)}", "error")
        return redirect(url_for("content_queue_page", client_id=item.get("client_id")))
    
@app.route("/client/<client_id>/content-draft", methods=["GET", "POST"])
@login_required
def generate_client_content_draft(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    default_query = ""
    if client.get("query_comparison", {}).get("rows"):
        default_query = client["query_comparison"]["rows"][0].get("query", "")

    if request.method == "POST":
        action_mode = safe_str(request.form.get("action_mode"))

        if action_mode == "prefill":
            form_data = {
                "target_query": safe_str(request.form.get("target_query")) or default_query,
                "content_type": safe_str(request.form.get("content_type", "service_page")) or "service_page",
                "brief_context": safe_str(request.form.get("brief_context")),
                "brand_context": safe_str(request.form.get("brand_context")) or client.get("notes", ""),
            }
            return render_template("content_draft_form.html", client=client, error=None, form_data=form_data)

        target_query = safe_str(request.form.get("target_query"))
        content_type = safe_str(request.form.get("content_type", "service_page")) or "service_page"
        brief_context = safe_str(request.form.get("brief_context"))
        brand_context = safe_str(request.form.get("brand_context"))

        if not target_query:
            return render_template(
                "content_draft_form.html",
                client=client,
                error="Target query is required.",
                form_data=request.form,
            )

        if not has_enough_credits(current_user, 2):
            flash("You don’t have enough credits to generate another draft.", "warning")
            return redirect(url_for("pricing_page"))

        if not spend_credits(current_user, 2, notes="Content draft generation"):
            flash("Unable to deduct credits for draft generation.", "warning")
            return redirect(url_for("pricing_page"))
        
        try:
            result = generate_content_draft(
                client_name=client.get("name", ""),
                website=client.get("website", ""),
                industry=client.get("industry", ""),
                location=client.get("location", ""),
                target_query=target_query,
                content_type=content_type,
                brief_context=brief_context,
                brand_context=brand_context,
            )
            flash("Content draft generated successfully.")
            return render_template("content_draft_result.html", client=client, result=result)

        except Exception as e:
            refund_credits(current_user, 2, notes="Refund for failed content draft generation")
            return render_template(
                "content_draft_form.html",
                client=client,
                error=f"Draft generation failed: {str(e)}",
                form_data=request.form,
            )

    prefill_query = safe_str(request.args.get("target_query"))
    prefill_brief_context = safe_str(request.args.get("brief_context"))
    prefill_brand_context = safe_str(request.args.get("brand_context"))

    form_data = {
        "target_query": prefill_query if prefill_query else default_query,
        "content_type": "service_page",
        "brief_context": prefill_brief_context,
        "brand_context": prefill_brand_context if prefill_brand_context else client.get("notes", ""),
    }
    return render_template("content_draft_form.html", client=client, error=None, form_data=form_data)

@app.route("/audit/<summary_filename>")
@login_required
def audit_summary(summary_filename):
    require_internal_access()   # 👈 ADD THIS LINE

    summary_path = get_summary_path(summary_filename)
    if not summary_path:
        abort(404)

    summary_data = load_json_file(summary_path)
    full_filename = get_matching_full_filename(summary_filename)
    return render_template("audit_summary.html", summary_filename=summary_filename, full_filename=full_filename, data=summary_data)


@app.route("/audit/<summary_filename>/full")
@login_required
def audit_full(summary_filename):
    require_internal_access()   # 👈 ADD THIS LINE

    full_path = get_full_path(summary_filename)
    if not full_path:
        abort(404)

    full_data = load_json_file(full_path)
    full_filename = get_matching_full_filename(summary_filename)
    return render_template("audit_full.html", summary_filename=summary_filename, full_filename=full_filename, data=full_data)


@app.route("/client/<client_id>/visibility")
@login_required
def client_visibility_page(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    latest_audit = client.get("latest_audit")
    comparison = client.get("comparison")
    query_comparison = client.get("query_comparison", {})
    query_rows = query_comparison.get("rows", [])
    query_summary = query_comparison.get("summary", {})

    return render_template(
        "client_visibility.html",
        client=client,
        latest_audit=latest_audit,
        comparison=comparison,
        query_rows=query_rows,
        query_summary=query_summary,
    )

@app.route("/prompt-detail")
@login_required
def prompt_detail_page():
    prompt_text = request.args.get("prompt", "").strip()
    project_domain = request.args.get("domain", "supportfast.ai").strip()
    selected_platform = request.args.get("platform", "ChatGPT").strip()
    selected_market = request.args.get("market", "United States (English)").strip()
    tracked_topic = request.args.get("topic", "Tracked prompts").strip()

    row = None
    if prompt_text:
        row = PromptTracking.query.filter_by(
            user_id=current_user.id,
            domain=project_domain,
            platform=selected_platform,
            market=selected_market,
            topic=tracked_topic,
            prompt=prompt_text,
        ).first()

    if row:
        visibility = row.visibility
        brand_mentioned = row.mentioned
        ranking_position = row.brand_position or "Not mentioned"
        last_checked = row.last_checked or "Unknown"
        change = row.change or "—"
        top_competitors = [row.top_competitor] if row.top_competitor else []
        recommended_actions = [
            f"Improve visibility for '{row.prompt}'",
            "Create a page directly matching this prompt intent",
            "Add stronger entity and trust signals to relevant pages",
            "Compare your answer coverage against the competitor being cited",
        ]
        ai_answer = (
            f"Current tracked visibility for this prompt is {row.visibility}. "
            f"Your brand mention status is {row.mentioned}. "
            f"Top competitor currently associated with this prompt is {row.top_competitor or 'unknown'}."
        )
    else:
        visibility = "Low"
        brand_mentioned = "No"
        ranking_position = "Not mentioned"
        last_checked = "Unknown"
        change = "New"
        top_competitors = ["tawk.to"]
        recommended_actions = [
            "Create a page directly answering this prompt",
            "Add stronger supporting content and FAQs",
            "Improve brand entity signals",
            "Track this prompt over time",
        ]
        ai_answer = "No saved AI answer is available for this prompt yet."

    source_domains = ["microsoft.com", "google.com", "g2.com"]

    return render_template(
        "prompt_detail.html",
        prompt_text=prompt_text or "Tracked prompt",
        project_domain=project_domain,
        selected_platform=selected_platform,
        selected_market=selected_market,
        tracked_topic=tracked_topic,
        visibility=visibility,
        brand_mentioned=brand_mentioned,
        ranking_position=ranking_position,
        last_checked=last_checked,
        change=change,
        top_competitors=top_competitors,
        source_domains=source_domains,
        recommended_actions=recommended_actions,
        ai_answer=ai_answer,
    )

@app.route("/save-prompts", methods=["POST"])
@login_required
def save_prompts():
    prompts = request.form.get("prompts", "").strip()
    domain = request.form.get("domain", "supportfast.ai").strip()
    platform = request.form.get("platform", "ChatGPT").strip()
    market = request.form.get("market", "United States (English)").strip()
    topic = request.form.get("topic", "Tracked prompts").strip()

    prompt_list = [p.strip() for p in prompts.splitlines() if p.strip()]

    if not prompt_list:
        flash("No prompts entered.", "warning")
        return redirect(url_for("position_tracking_page"))

    def guess_competitor(prompt: str) -> str:
        p = prompt.lower()
        if "whatsapp" in p:
            return "wati.io"
        if "knowledge base" in p or "help center" in p:
            return "tawk.to"
        if "booking" in p or "calendar" in p or "appointment" in p:
            return "botpenguin.com"
        if "chat" in p or "messaging" in p:
            return "tawk.to"
        return "tawk.to"

    created_count = 0

    for i, prompt in enumerate(prompt_list):
        existing = PromptTracking.query.filter_by(
            user_id=current_user.id,
            domain=domain,
            platform=platform,
            market=market,
            topic=topic,
            prompt=prompt,
        ).first()

        if existing:
            existing.last_checked = "Updated now"
            apply_prompt_score(existing)
            continue

        row = PromptTracking(
            user_id=current_user.id,
            domain=domain,
            platform=platform,
            market=market,
            topic=topic or "Tracked prompts",
            prompt=prompt,
            status="Tracking",
            visibility="Medium" if i == 0 else "Low",
            mentioned="Sometimes" if i == 0 else "No",
            top_competitor=guess_competitor(prompt),
            last_checked="Just added",
            change="New",
        )

        apply_prompt_score(row)
        db.session.add(row)
        created_count += 1

    db.session.commit()

    if created_count > 0:
        flash(f"{created_count} prompts added to tracking.", "success")
    else:
        flash("These prompts were already being tracked.", "info")

    return redirect(url_for(
        "position_tracking_page",
        domain=domain,
        platform=platform,
        market=market,
        topic=topic,
    ))

@app.route("/position-tracking")
@login_required
def position_tracking_page():
    domain = request.args.get("domain", "").strip()
    platform = request.args.get("platform", "ChatGPT").strip()
    market = request.args.get("market", "United States (English)").strip()
    topic = request.args.get("topic", "").strip()

    query = PromptTracking.query.filter_by(user_id=current_user.id)

    if domain:
        query = query.filter_by(domain=domain)
    if platform:
        query = query.filter_by(platform=platform)
    if market:
        query = query.filter_by(market=market)
    if topic:
        query = query.filter_by(topic=topic)

    rows = query.order_by(PromptTracking.created_at.desc()).all()

    tracked_prompts = []
    for row in rows:
        tracked_prompts.append({
            "id": row.id,
            "prompt": row.prompt,
            "status": row.status,
            "visibility": row.visibility,
            "mentioned": row.mentioned,
            "top_competitor": row.top_competitor or "—",
            "last_checked": row.last_checked,
            "change": row.change,
            "prompt_score": row.prompt_score,
            "score_band": row.score_band,
            "opportunity_label": row.opportunity_label,
            "brand_position": row.brand_position,
            "competitor_count": row.competitor_count,
            "source_support": row.source_support,
        })

    mentioned_count = sum(1 for row in tracked_prompts if row["mentioned"] == "Yes")
    partial_count = sum(1 for row in tracked_prompts if row["mentioned"] == "Sometimes")
    low_visibility_count = sum(1 for row in tracked_prompts if row["visibility"] == "Low")
    highest_competitor = tracked_prompts[0]["top_competitor"] if tracked_prompts else "—"
    best_next_move = "Build content for missing prompts" if low_visibility_count > 0 else "Keep tracking visibility"

    return render_template(
        "position_tracking.html",
        project_domain=domain or "supportfast.ai",
        selected_platform=platform,
        selected_market=market,
        tracked_topic=topic or "Tracked prompts",
        total_prompts=len(tracked_prompts),
        tracked_prompts=tracked_prompts,
        tracking_ready=len(tracked_prompts) > 0,
        progress_percent=100 if tracked_prompts else 0,
        mentioned_count=mentioned_count,
        partial_count=partial_count,
        low_visibility_count=low_visibility_count,
        highest_competitor=highest_competitor,
        best_next_move=best_next_move,
    )


@app.route("/client/<client_id>/competitors")
@login_required
def client_competitors_page(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)
    return render_template("client_competitors.html", client=client)


@app.route("/client/<client_id>/actions")
@login_required
def client_actions_page(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)
    return render_template("client_actions.html", client=client)


@app.route("/client/<client_id>/history")
@login_required
def client_history_page(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)
    return render_template("client_history.html", client=client)


@app.route("/content")
@app.route("/content-queue")
@login_required
def content_queue_page():
    client_id = request.args.get("client_id", "").strip()
    incoming_query = request.args.get("query", "").strip()
    incoming_topic = request.args.get("topic", "").strip()
    incoming_source = request.args.get("source", "").strip()

    clients = build_client_views()
    view_mode = get_view_mode(current_user)

    if not client_id and view_mode == "single":
        focused_client = get_focused_client_for_user(current_user)
        if focused_client:
            client_id = focused_client["id"]

    selected_client_id = client_id if client_id else None

    items = get_queue_items(
        client_id=selected_client_id,
        user_id=current_user.id,
    )

    for item in items:
        item["next_action"] = get_next_action(item)

    stats = {
        "queued": len([i for i in items if (i.get("status") or "").lower() in ["queued", "pending"]]),
        "in_progress": len([i for i in items if (i.get("status") or "").lower() in ["in_progress", "in-progress", "draft_generated"]]),
        "ready": len([i for i in items if (i.get("status") or "").lower() in ["ready", "brief_generated", "brief ready"]]),
        "published": len([i for i in items if (i.get("status") or "").lower() == "published"]),
    }

    selected_client = None
    if selected_client_id:
        selected_client = next(
            (client for client in clients if str(client.get("id")) == str(selected_client_id)),
            None
        )

    return render_template(
        "content_queue.html",
        queue_items=items,
        selected_client_id=selected_client_id,
        selected_client=selected_client,
        stats=stats,
        incoming_query=incoming_query,
        incoming_topic=incoming_topic,
        incoming_source=incoming_source,
    )

@app.route("/content-queue/<item_id>/status", methods=["POST"])
@login_required
def update_content_queue_status(item_id):
    new_status = request.form.get("status", "pending").strip()
    item = update_queue_item_status(item_id, new_status, user_id=current_user.id)

    if not item:
        abort(404)

    client_id = request.form.get("client_id", "").strip()
    flash("Queue item status updated.")

    if client_id:
        return redirect(url_for("content_queue_page", client_id=client_id))
    return redirect(url_for("content_queue_page"))

@app.route("/client/<client_id>/save-brief", methods=["POST"])
@login_required
def save_generated_brief(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    target_query = request.form.get("target_query", "").strip()
    content_type = request.form.get("content_type", "").strip()
    brief_text = request.form.get("brief_text", "").strip()
    title = f"Brief: {target_query}" if target_query else "Content Brief"

    add_queue_item(
        client_id=client.get("id"),
        client_name=client.get("name"),
        target_query=target_query,
        content_type=content_type,
        item_type="brief",
        title=title,
        content=brief_text,
        status="brief_generated",
        priority="medium",
        source="manual",
        user_id=current_user.id,
    )
    flash("Brief saved to content queue.")
    return redirect(url_for("content_queue_page", client_id=client.get("id")))


@app.route("/client/<client_id>/save-draft", methods=["POST"])
@login_required
def save_generated_draft(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    target_query = safe_str(request.form.get("target_query"))
    content_type = safe_str(request.form.get("content_type"))    
    draft_text = request.form.get("draft_text", "").strip()
    title = f"Draft: {target_query}" if target_query else "Content Draft"

    add_queue_item(
        client_id=client.get("id"),
        client_name=client.get("name"),
        target_query=target_query,
        content_type=content_type,
        item_type="draft",
        title=title,
        content=draft_text,
        status="draft_generated",
        priority="medium",
        source="manual",
        user_id=current_user.id,
    )

    flash("Draft saved to content queue.")
    return redirect(url_for("content_queue_page", client_id=client.get("id")))

# =========================
# API routes
# =========================

@app.route("/api/audits")
@login_required
def api_audits():
    all_audits = get_saved_audits(user_id=current_user.id)
    search_term = request.args.get("q", "").strip()
    audit_type = request.args.get("type", "all").strip().lower()
    sort_by = request.args.get("sort", "saved_at").strip()
    order = request.args.get("order", "desc").strip().lower()

    audits = filter_audits(all_audits, search_term=search_term, audit_type=audit_type)
    audits = sort_audits(audits, sort_by=sort_by, order=order)

    return jsonify({"count": len(audits), "total_count": len(all_audits), "items": audits})


@app.route("/api/clients")
@login_required
def api_clients():
    clients = build_client_views()
    return jsonify({"count": len(clients), "items": clients})

@app.route("/content/brief/new")
@login_required

def generate_content_brief_page():
    return redirect(url_for("content_queue_page"))

@app.route("/api/client/<client_id>")
@login_required

def api_client_detail(client_id):
    client = get_client_by_id(client_id)
    if not client:
        return jsonify({"error": "Client not found"}), 404
    return jsonify(client)

@app.route("/client/<client_id>/presentation")
@login_required
def client_presentation_page(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    view_mode = get_view_mode(current_user)
    can_use_presentation_mode = view_mode in ["multi", "admin"]

    if not can_use_presentation_mode:
        flash("Presentation mode is available for agency workspaces.", "warning")
        return redirect(url_for("client_detail", client_id=client_id))

    return render_template(
        "client_presentation.html",
        client=client,
        can_use_presentation_mode=can_use_presentation_mode,
    )

@app.route("/client/<client_id>/growth-plan")
@login_required
def client_growth_plan(client_id):

    # 🔥 DIRECT DB lookup instead of view builder
    row = Client.query.filter_by(
        slug=client_id,
        user_id=current_user.id
    ).first()

    if not row:
        return f"❌ Client not found or access denied: {client_id}", 404

    # convert to dict (same format your templates expect)
    client = serialize_client_row(row)

    # 🔥 rebuild full view manually
    all_clients = build_client_views()
    full_client = next((c for c in all_clients if c["id"] == client_id), None)

    if not full_client:
        return f"❌ Client exists but no audit data found: {client_id}", 404

    latest_audit = full_client.get("latest_audit")
    actions = full_client.get("recommended_actions", [])
    comparison = full_client.get("comparison")

    # summary logic
    if comparison:
        overall_change = comparison.get("overall_change", "unchanged")
        score = comparison.get("latest", {}).get("normalized_score", 0)
        summary = f"Overall performance has {overall_change}. Current score: {score}."
    elif latest_audit:
        summary = f"Score: {latest_audit.get('normalized_score', 0)}."
    else:
        summary = "No audit yet. Run an audit first."

    return render_template(
        "client_growth_plan.html",
        client=full_client,
        audit=latest_audit,
        actions=actions,
        summary=summary,
        audit_count=full_client.get("audit_count", 0),
    )
        
@app.route("/start-audit")
@login_required
def start_audit():
    view_mode = get_view_mode(current_user)
    clients = build_client_views()

    if not clients:
        return redirect(url_for("create_client"))

    focused_client = get_focused_client_for_user(current_user)

    if view_mode == "single" and focused_client:
        return redirect(url_for("new_audit", client_id=focused_client["id"]))

    if len(clients) == 1:
        return redirect(url_for("new_audit", client_id=clients[0]["id"]))

    return redirect(url_for("clients_page"))

@app.route("/audit/new", methods=["GET", "POST"])
@login_required
def new_audit():
    clients = build_client_views()
    view_mode = get_view_mode(current_user)

    if not clients:
        flash("Create a client first.", "warning")
        return redirect(url_for("create_client"))

    focused_client = get_focused_client_for_user(current_user)

    if request.method == "POST":
        client_id = request.form.get("client_id", "").strip()
        website = request.form.get("website", "").strip()
        industry = request.form.get("industry", "").strip()
        location = request.form.get("location", "").strip()
        topic = request.form.get("topic", "").strip()
        audit_type = request.form.get("audit_type", "quick").strip()
        notes = request.form.get("notes", "").strip()

        if not client_id:
            if view_mode == "single" and focused_client:
                client_id = str(focused_client["id"])
            elif len(clients) == 1:
                client_id = str(clients[0]["id"])

        if not client_id:
            return render_template(
                "new_audit.html",
                clients=clients,
                preselected_client_id=str(focused_client["id"]) if (view_mode == "single" and focused_client) else (clients[0]["id"] if len(clients) == 1 else None),
                form_data=request.form,
                error="Please choose a workspace.",
                view_mode=view_mode,
            )

        if not website or not industry or not location:
            return render_template(
                "new_audit.html",
                clients=clients,
                preselected_client_id=str(focused_client["id"]) if (view_mode == "single" and focused_client) else (clients[0]["id"] if len(clients) == 1 else None),
                form_data=request.form,
                error="Website, industry, and location are required.",
                view_mode=view_mode,
            )

        if not has_enough_credits(current_user, 1):
            flash("You don’t have enough credits to run another audit.", "warning")
            return redirect(url_for("pricing_page"))

        if not spend_credits(current_user, 1, notes="New audit run"):
            flash("Unable to deduct credits for audit.", "warning")
            return redirect(url_for("pricing_page"))

        try:
            run_audit_for_input(
                website=website,
                industry=industry,
                location=location,
                audit_type=audit_type,
                topic=topic if topic else None,
                client_id=client_id,
                client_name=None,
                user_id=current_user.id,
            )

            flash("Audit completed successfully.", "success")
            return redirect(url_for("client_detail", client_id=client_id))

        except Exception as e:
            refund_credits(current_user, 1, notes="Refund for failed new audit")
            return render_template(
                "new_audit.html",
                clients=clients,
                error=f"Audit failed: {str(e)}",
                form_data=request.form,
                view_mode=view_mode,
            )
    requested_client_id = request.args.get("client_id", "").strip()

    prefilled_client = None
    preselected_client_id = None

    if requested_client_id:
        prefilled_client = next((c for c in clients if str(c["id"]) == requested_client_id), None)
        if prefilled_client:
            preselected_client_id = str(prefilled_client["id"])

    if not prefilled_client:
        if view_mode == "single" and focused_client:
            prefilled_client = focused_client
            preselected_client_id = str(focused_client["id"])
        elif len(clients) == 1:
            prefilled_client = clients[0]
            preselected_client_id = str(clients[0]["id"])

    form_data = {
        "client_id": preselected_client_id or "",
        "website": prefilled_client.get("website", "") if prefilled_client else "",
        "industry": prefilled_client.get("industry", "") if prefilled_client else "",
        "location": prefilled_client.get("location", "") if prefilled_client else "",
        "topic": prefilled_client.get("industry", "") if prefilled_client else "",
        "audit_type": "quick",
        "notes": "",
    }

    return render_template(
        "new_audit.html",
        clients=clients,
        preselected_client_id=preselected_client_id,
        form_data=form_data,
        error=None,
        view_mode=view_mode,
    )

@app.route("/api/audit/<summary_filename>/full")
@login_required
def api_audit_full(summary_filename):
    full_path = get_full_path(summary_filename)
    if not full_path:
        return jsonify({"error": "Full file not found"}), 404

    full_data = load_json_file(full_path)
    full_filename = get_matching_full_filename(summary_filename)
    return jsonify({"summary_filename": summary_filename, "full_filename": full_filename, "data": full_data})


# =========================
# Template helpers
# =========================

@app.template_filter("pretty_datetime")
def pretty_datetime(value):
    if not value:
        return "N/A"
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return value



@app.context_processor
def inject_template_globals():
    wallet_balance = 0
    credit_balance_numeric = 0
    has_unlimited_credits = False
    view_mode = "single"
    can_use_presentation_mode = False
    workspace_count = 0
    workspace_limit = 0
    can_add_workspace = False
    focused_client = None

    if current_user.is_authenticated:
        has_unlimited_credits = user_has_unlimited_credits(current_user)
        view_mode = get_view_mode(current_user)
        can_use_presentation_mode = view_mode in ["multi", "admin"]

        workspace_count = get_workspace_count(current_user.id)
        workspace_limit = get_workspace_limit(current_user)
        can_add_workspace = workspace_limit is None or workspace_count < workspace_limit
        focused_client = get_focused_client_for_user(current_user)

        if has_unlimited_credits:
            wallet_balance = "Unlimited"
            credit_balance_numeric = 999999
        elif getattr(current_user, "wallet", None):
            wallet_balance = current_user.wallet.balance
            credit_balance_numeric = current_user.wallet.balance

    return {
        "HELP_GLOSSARY": HELP_GLOSSARY,
        "wallet_balance": wallet_balance,
        "credit_balance_numeric": credit_balance_numeric,
        "has_unlimited_credits": has_unlimited_credits,
        "view_mode": view_mode,
        "can_use_presentation_mode": can_use_presentation_mode,
        "workspace_count": workspace_count,
        "workspace_limit": workspace_limit,
        "can_add_workspace": can_add_workspace,
        "focused_client": focused_client,
        "can_run_audit": (
            current_user.is_authenticated and (
                has_unlimited_credits or credit_balance_numeric >= 1
            )
        ),
        "can_generate_brief": (
            current_user.is_authenticated and (
                has_unlimited_credits or credit_balance_numeric >= 1
            )
        ),
        "can_generate_draft": (
            current_user.is_authenticated and (
                has_unlimited_credits or credit_balance_numeric >= 2
            )
        ),
    }

@app.route("/aeo-agency")
def aeo_agency_page():
    return render_template("landing_aeo.html")

def render_settings_section(section_name: str):
    return render_template(
        "settings.html",
        active_settings_section=section_name
    )

@app.route("/settings")
@login_required
def settings_page():
    return render_settings_section("profile")

@app.route("/settings/account")
@login_required
def settings_account():
    return render_settings_section("account")

@app.route("/settings/billing")
@login_required
def settings_billing():
    return render_settings_section("billing")

@app.route("/settings/credits")
@login_required
def settings_credits():
    return render_settings_section("credits")

@app.route("/settings/referrals")
@login_required
def settings_referrals():
    return render_settings_section("referrals")

@app.route("/settings/preferences")
@login_required
def settings_preferences():
    return render_settings_section("preferences")

@app.route("/settings/team")
@login_required
def settings_team():
    return render_settings_section("team")

if __name__ == "__main__":
    ensure_data_dirs()
    with app.app_context():
        db.create_all()
    print("Starting Flask app...")
    app.run(host="127.0.0.1", port=5001, debug=True)