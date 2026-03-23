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

app = Flask(__name__)
print("Flask app initialized")

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-to-a-random-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# =========================
# General helpers
# =========================

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

            if user_id is not None and data.get("user_id") != user_id:
                continue

            website = data.get("website", "N/A")
            audits.append({
                "filename": filename,
                "website": website,
                "website_normalized": normalize_website(website),
                "client_id": data.get("client_id"),
                "client_name": data.get("client_name"),
                "audit_type": data.get("audit_type", "N/A"),
                "saved_at": data.get("saved_at", ""),
                "verdict": data.get("summary", {}).get("verdict", "N/A"),
                "opportunity_level": data.get("summary", {}).get("opportunity_level", "N/A"),
                "normalized_score": data.get("scores", {}).get("normalized_score", 0),
                "visibility_score": data.get("scores", {}).get("visibility_score", 0),
                "content_score": data.get("scores", {}).get("content_score", 0),
                "schema_score": data.get("scores", {}).get("schema_score", 0),
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


def build_recommended_actions(client, latest_audit, query_comparison):
    actions = []
    if not latest_audit:
        return actions

    visibility_score = latest_audit.get("visibility_score", 0)
    content_score = latest_audit.get("content_score", 0)
    schema_score = latest_audit.get("schema_score", 0)

    if visibility_score <= 5:
        actions.append({
            "priority": "high",
            "title": "Improve brand visibility in AI answers",
            "issue": "Brand visibility score is very low.",
            "recommended_action": "Create brand-led and comparison-style pages targeting high-intent commercial queries.",
            "support_signal": "Add stronger entity mentions, FAQ coverage, and external brand references.",
        })

    if content_score <= 3:
        actions.append({
            "priority": "high",
            "title": "Expand content coverage",
            "issue": "Content score is low.",
            "recommended_action": "Publish service pages, FAQs, and educational content around target search intents.",
            "support_signal": "Map each content piece to a key commercial or question-based query cluster.",
        })

    if schema_score <= 3:
        actions.append({
            "priority": "medium",
            "title": "Strengthen structured data",
            "issue": "Schema score is weak.",
            "recommended_action": "Add Organization, FAQPage, LocalBusiness, and relevant service schema markup.",
            "support_signal": "Validate schema and align page markup with the business entity.",
        })

    rows = query_comparison.get("rows", []) if query_comparison else []
    weak_rows = [row for row in rows if not row.get("latest_brand_mentioned", False)]
    weak_rows = sorted(weak_rows, key=lambda x: x.get("latest_score", 0))

    for row in weak_rows[:5]:
        query = row.get("query", "Unknown query")
        competitors = row.get("latest_competitors", [])
        competitor_text = ", ".join(competitors[:3]) if competitors else "competing providers"

        actions.append({
            "priority": "high",
            "title": f"Target missed query: {query}",
            "issue": "Brand is not mentioned for this tracked query.",
            "recommended_action": f"Create or improve a landing page, FAQ section, or comparison article focused on '{query}'.",
            "support_signal": f"Strengthen authority signals and competitor-differentiation against {competitor_text}.",
        })

    seen = set()
    unique_actions = []
    for action in actions:
        if action["title"] not in seen:
            unique_actions.append(action)
            seen.add(action["title"])

    priority_order = {"high": 0, "medium": 1, "low": 2}
    unique_actions.sort(key=lambda x: priority_order.get(x["priority"], 99))
    return unique_actions


def build_client_views():
    clients = load_clients(user_id=current_user.id)
    audits = get_saved_audits(user_id=current_user.id)

    client_views = []

    for client in clients:
        matched_audits = [
            audit for audit in audits
            if (
                audit.get("client_id") == client.get("id")
                or (
                    not audit.get("client_id")
                    and audit.get("website_normalized") == client.get("website_normalized")
                )
            )
        ]

        matched_audits = sort_audits(matched_audits, sort_by="saved_at", order="desc")
        latest_audit = matched_audits[0] if matched_audits else None
        previous_audit = matched_audits[1] if len(matched_audits) > 1 else None
        comparison = compare_audits(latest_audit, previous_audit)
        query_comparison = build_query_level_comparison(latest_audit, previous_audit)
        recommended_actions = build_recommended_actions(client, latest_audit, query_comparison)

        client_views.append({
            **client,
            "audit_count": len(matched_audits),
            "latest_audit": latest_audit,
            "previous_audit": previous_audit,
            "comparison": comparison,
            "query_comparison": query_comparison,
            "recommended_actions": recommended_actions,
            "audits": matched_audits,
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

    if user.is_white_label_enabled:
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

    if current_user.role == "admin" or current_user.plan == "dev_unlimited":
        return

    # allow your dev email
    if current_user.email == "pypteltd@gmail.com":
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
        search_term = request.args.get("q", "").strip()
        audit_type = request.args.get("type", "all").strip().lower()
        sort_by = request.args.get("sort", "saved_at").strip()
        order = request.args.get("order", "desc").strip().lower()

        audits = filter_audits(all_audits, search_term=search_term, audit_type=audit_type)
        audits = sort_audits(audits, sort_by=sort_by, order=order)

        return render_template(
            "dashboard.html",
            audits=audits,
            total_audits=len(all_audits),
            search_term=search_term,
            selected_type=audit_type,
            selected_sort=sort_by,
            selected_order=order,
        )

    return redirect(url_for("login"))


@app.route("/clients")
@login_required
def clients_page():
    return render_template("clients.html", clients=build_client_views())

@app.route("/client/<client_id>/report")
@login_required
def report_page(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)
    return render_template("report_page.html", client=client)

@app.route("/clients/new", methods=["GET", "POST"])
@login_required
def create_client():
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

        client = add_client({
            "name": name,
            "website": website,
            "industry": industry,
            "location": location,
            "owner_type": owner_type,
            "notes": notes,
        }, user_id=current_user.id)

        flash("Client workspace created successfully.")
        return redirect(url_for("client_detail", client_id=client["id"]))

    return render_template("client_form.html", error=None, form_data={}, mode="create", client=None)

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

        login_user(user)
        flash("Logged in successfully.")
        return redirect(url_for("index"))

    return render_template("login.html", error=None)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.")
    return redirect(url_for("login"))


@app.route("/client/<client_id>/run-audit", methods=["GET", "POST"])
@login_required
def run_client_audit(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    if request.method == "POST":
        website = request.form.get("website", "").strip()
        industry = request.form.get("industry", "").strip()
        location = request.form.get("location", "").strip()
        topic = request.form.get("topic", "").strip()
        audit_type = request.form.get("audit_type", "free").strip()

        if not website or not industry or not location:
            return render_template("run_audit.html", client=client, error="Website, industry, and location are required.", form_data=request.form)

        if not spend_credits(current_user, 1, notes="Audit generation"):
            return render_template("run_audit.html", client=client, error="Not enough credits.", form_data=request.form)

        try:
            result = run_audit_for_input(
                website=website,
                industry=industry,
                location=location,
                audit_type=audit_type,
                topic=topic if topic else None,
                client_id=client.get("id"),
                client_name=client.get("name"),
                user_id=current_user.id,
            )
            award_referral_if_qualified(current_user)
            flash("Audit completed successfully.")
            return render_template("run_audit_success.html", client=client, result=result)
        except Exception as e:
            refund_credits(current_user, 1, notes="Refund for failed audit generation")
            return render_template("run_audit.html", client=client, error=f"Audit failed: {str(e)}", form_data=request.form)

    form_data = {
        "website": client.get("website", ""),
        "industry": client.get("industry", ""),
        "location": client.get("location", ""),
        "topic": client.get("industry", ""),
        "audit_type": "free",
    }
    return render_template("run_audit.html", client=client, error=None, form_data=form_data)


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
            return render_template("content_brief_form.html", client=client, error="Target query is required.", form_data=request.form)

        if not spend_credits(current_user, 1, notes="Content brief generation"):
            return render_template("content_brief_form.html", client=client, error="Not enough credits.", form_data=request.form)

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
            return render_template("content_brief_result.html", client=client, result=result)
        except Exception as e:
            refund_credits(current_user, 1, notes="Refund for failed content brief generation")
            return render_template("content_brief_form.html", client=client, error=f"Brief generation failed: {str(e)}", form_data=request.form)

    prefill_query = safe_str(request.args.get("target_query"))
    prefill_context = safe_str(request.args.get("brand_context"))

    form_data = {
        "target_query": prefill_query if prefill_query else default_query,
        "content_type": "service_page",
        "brand_context": prefill_context if prefill_context else client.get("notes", ""),
    }
    return render_template("content_brief_form.html", client=client, error=None, form_data=form_data)

@app.route("/generate-brief/<item_id>")
@login_required
def generate_brief_from_queue(item_id):
    item = get_queue_item_by_id(item_id, user_id=current_user.id)

    if not item:
        flash("Queue item not found.", "error")
        return redirect(url_for("content_queue_page"))

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
            flash("Brief was generated but could not be saved to the queue item.", "error")
            return redirect(url_for("content_queue_page", client_id=item.get("client_id")))

        flash("Brief generated successfully.", "success")
        return redirect(url_for("content_queue_page", client_id=item.get("client_id")))

    except Exception as e:
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
            return render_template("content_draft_form.html", client=client, error="Target query is required.", form_data=request.form)

        if not spend_credits(current_user, 2, notes="Content draft generation"):
            return render_template("content_draft_form.html", client=client, error="Not enough credits.", form_data=request.form)

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
            return render_template("content_draft_form.html", client=client, error=f"Draft generation failed: {str(e)}", form_data=request.form)

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
    return render_template("client_visibility.html", client=client)


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
    clients = build_client_views()
    view_mode = get_view_mode(current_user)

    if not client_id and view_mode == "single" and len(clients) == 1:
        client_id = clients[0]["id"]

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

    return render_template(
        "content_queue.html",
        queue_items=items,
        selected_client_id=selected_client_id,
        stats=stats,
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

    target_query = request.form.get("target_query", "").strip()
    content_type = request.form.get("content_type", "").strip()
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

@app.route("/api/audit/<summary_filename>/summary")
@login_required
def api_audit_summary(summary_filename):
    summary_path = get_summary_path(summary_filename)
    if not summary_path:
        return jsonify({"error": "Summary file not found"}), 404

    summary_data = load_json_file(summary_path)
    return jsonify({"filename": summary_filename, "data": summary_data})

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
        
@app.route("/audit/new")
@login_required
def new_audit():
    clients = build_client_views()

    if not clients:
        flash("Create a client first.", "warning")
        return redirect(url_for("create_client"))

    if len(clients) == 1:
        return redirect(url_for("run_client_audit", client_id=clients[0]["id"]))

    return redirect(url_for("clients_page"))

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
    has_unlimited_credits = False
    view_mode = "single"
    can_use_presentation_mode = False

    if current_user.is_authenticated:
        has_unlimited_credits = user_has_unlimited_credits(current_user)
        view_mode = get_view_mode(current_user)
        can_use_presentation_mode = view_mode in ["multi", "admin"]

        if has_unlimited_credits:
            wallet_balance = "Unlimited"
        elif getattr(current_user, "wallet", None):
            wallet_balance = current_user.wallet.balance

    return {
        "HELP_GLOSSARY": HELP_GLOSSARY,
        "wallet_balance": wallet_balance,
        "has_unlimited_credits": has_unlimited_credits,
        "view_mode": view_mode,
        "can_use_presentation_mode": can_use_presentation_mode,
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
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)

