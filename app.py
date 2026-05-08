from flask_migrate import Migrate
from content_queue import (
    add_queue_item,
    get_queue_items,
    get_queue_item_by_id,
    get_next_action,
    update_queue_item_status,
    update_queue_item_content,
    update_queue_item_details,
    update_queue_item_schedule,
    update_queue_item_og_image,
    update_queue_item_webflow_export,
    delete_queue_item,
    transition_queue_item,
)
from help_content import HELP_GLOSSARY
from content_draft_generator import generate_content_draft
from content_brief_generator import generate_content_brief
from audit_runner import run_audit_for_input
import json
import logging
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm.attributes import flag_modified
from action_engine import (
    build_recommended_actions,
    build_content_opportunities,
)
from next_action_engine import build_next_best_action
from growth_calendar import weekly_growth_recommendations
from query_idea_generator import generate_query_ideas
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
    make_response,
)
from datetime import datetime
from tavily import TavilyClient
from urllib.parse import urlparse
from website_page_builder import generate_structured_website_page
from webflow_integration import (
    WebflowAPIError,
    WebflowConfigError,
    export_project_to_webflow,
    get_webflow_setup_status,
    is_webflow_configured,
    verify_webflow_connection,
)
import os
import csv
from dotenv import load_dotenv

load_dotenv()


def _check_env_health():
    required = {
        "OPENAI_API_KEY": "AI brief/draft/page generation",
        "TAVILY_API_KEY": "competitor and web research",
    }
    optional = {
        "WEBFLOW_API_TOKEN": "Webflow publishing",
        "WEBFLOW_SITE_ID": "Webflow publishing",
        "WEBFLOW_COLLECTION_ID": "legacy single-collection export",
        "WEBFLOW_BLOG_COLLECTION_ID": "blog publishing",
        "WEBFLOW_FAQ_COLLECTION_ID": "FAQ publishing",
        "WEBFLOW_SERVICE_COLLECTION_ID": "service-page publishing",
        "WEBFLOW_LOCATION_COLLECTION_ID": "location-page publishing",
        "PLACID_API_TOKEN": "visual generation (OG images, banners)",
        "PLACID_TEMPLATE_UUID_OG": "OG image template for queue items",
    }

    missing_required = [(k, why) for k, why in required.items() if not os.getenv(k)]
    missing_optional = [(k, why) for k, why in optional.items() if not os.getenv(k)]

    if missing_required:
        print("WARNING: required env vars are missing — features will fail when invoked:")
        for key, why in missing_required:
            print(f"  - {key}  ({why})")
    if missing_optional:
        print("INFO: optional env vars not set — related features are disabled:")
        for key, why in missing_optional:
            print(f"  - {key}  ({why})")
    if not (missing_required or missing_optional):
        print("Env check: all known keys present.")


_check_env_health()


app = Flask(__name__)
print("Flask app initialized")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY", "change-this-to-a-random-secret-key"
)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL", "sqlite:///app.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join(
    "static", "uploads", "workspace_logos"
)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

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
    referred_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
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
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False
    )
    balance = db.Column(db.Integer, default=0, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class CreditTransaction(db.Model):
    __tablename__ = "credit_transactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    balance_after = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )


class Referral(db.Model):
    __tablename__ = "referrals"

    id = db.Column(db.Integer, primary_key=True)
    referrer_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    referred_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    referral_code = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(50), default="pending", nullable=False)
    reward_amount_referrer = db.Column(db.Integer, default=0, nullable=False)
    reward_amount_referred = db.Column(db.Integer, default=0, nullable=False)
    qualified_at = db.Column(db.DateTime, nullable=True)
    rewarded_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(255), nullable=False, index=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )

    name = db.Column(db.String(255), nullable=False)
    website = db.Column(db.String(500), nullable=False)
    website_normalized = db.Column(db.String(500), nullable=False, index=True)

    industry = db.Column(db.String(255), nullable=True)
    location = db.Column(db.String(255), nullable=True)
    owner_type = db.Column(db.String(100), nullable=False, default="company")
    notes = db.Column(db.Text, nullable=True)
    logo_filename = db.Column(db.String(255), nullable=True)

    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint("user_id", "slug", name="uq_clients_user_slug"),
    )


class PromptTracking(db.Model):
    __tablename__ = "prompt_tracking"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )

    domain = db.Column(db.String(255), nullable=True, index=True)
    platform = db.Column(db.String(100), nullable=True, index=True)
    market = db.Column(db.String(255), nullable=True, index=True)
    topic = db.Column(db.String(255), nullable=True, index=True)

    prompt = db.Column(db.Text, nullable=False)

    status = db.Column(db.String(50), default="Tracking", nullable=False)
    visibility = db.Column(db.String(50), default="Low", nullable=False)
    mentioned = db.Column(db.String(50), default="No", nullable=False)
    top_competitor = db.Column(db.String(255), nullable=True)

    last_checked = db.Column(
        db.String(100), default="Just added", nullable=True
    )
    change = db.Column(db.String(50), default="New", nullable=True)

    prompt_score = db.Column(db.Integer, default=0, nullable=False)
    score_band = db.Column(db.String(50), default="Weak", nullable=True)
    opportunity_label = db.Column(
        db.String(100), default="High opportunity", nullable=True
    )
    brand_position = db.Column(
        db.String(100), default="Not mentioned", nullable=True
    )
    competitor_count = db.Column(db.Integer, default=0, nullable=False)
    source_support = db.Column(db.String(100), default="Low", nullable=True)

    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )


class GeneratedWebsiteProject(db.Model):
    __tablename__ = "generated_website_projects"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False
    )

    title = db.Column(db.String(255), nullable=False)
    theme = db.Column(db.String(100), default="professional_services")
    status = db.Column(db.String(40), default="draft")

    blueprint_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class GeneratedWebsitePage(db.Model):
    __tablename__ = "generated_website_pages"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("generated_website_projects.id"),
        nullable=True,
    )

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=True
    )

    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(220), nullable=False)
    page_type = db.Column(db.String(80), default="service")
    status = db.Column(db.String(40), default="draft")

    page_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class WebflowExport(db.Model):
    """
    Tracks exports of DarInsights content to Webflow CMS.
    
    Allows monitoring which content has been exported, its status,
    and whether it's published or still in draft on Webflow.
    
    Related to:
    - GeneratedWebsitePage (page content exports)
    - Content brief/draft results (blog post exports)
    - FAQ items (FAQ collection exports)
    - Service items (service collection exports)
    """
    __tablename__ = "webflow_exports"

    id = db.Column(db.Integer, primary_key=True)
    
    # User and workspace context
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=True)
    
    # What was exported
    content_type = db.Column(
        db.String(50),
        nullable=False,
        comment="Type of content: blog, faq, service, location, page"
    )
    local_source_type = db.Column(
        db.String(50),
        nullable=False,
        comment="Source type: generated_page, content_brief, faq_item, service_item"
    )
    local_source_id = db.Column(
        db.Integer,
        nullable=False,
        comment="ID of the source content (e.g., GeneratedWebsitePage.id)"
    )
    
    # Webflow destination
    webflow_site_id = db.Column(db.String(100), nullable=False)
    webflow_collection_id = db.Column(db.String(100), nullable=False)
    webflow_item_id = db.Column(
        db.String(100),
        nullable=True,
        comment="Webflow item ID after creation/update"
    )
    
    # Status tracking
    status = db.Column(
        db.String(50),
        default="draft",
        comment="Status: draft, exported, published, failed"
    )
    error_message = db.Column(
        db.Text,
        nullable=True,
        comment="Error message if status is failed"
    )
    
    # Metadata
    field_mapping = db.Column(
        db.JSON,
        nullable=True,
        comment="Map of field_slug -> value that was sent to Webflow"
    )
    webflow_response = db.Column(
        db.JSON,
        nullable=True,
        comment="Last response from Webflow API"
    )
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# =========================
# General helpers
# =========================


def build_ai_brief_context(client, target_query, latest_audit=None):
    client_name = safe_str(client.get("name"))
    industry = safe_str(client.get("industry"))
    location = safe_str(client.get("location"))
    website = safe_str(client.get("website"))
    notes = safe_str(client.get("notes"))

    competitor_names = []
    content_gaps = []

    if latest_audit:
        for c in (latest_audit.get("top_competitors") or [])[:3]:
            if isinstance(c, dict):
                name = safe_str(c.get("name"))
                if name:
                    competitor_names.append(name)
            else:
                name = safe_str(c)
                if name:
                    competitor_names.append(name)

        for gap in (latest_audit.get("top_content_gaps") or [])[:3]:
            if isinstance(gap, dict):
                label = safe_str(
                    gap.get("title") or gap.get("query") or gap.get("label")
                )
                if label:
                    content_gaps.append(label)
            else:
                label = safe_str(gap)
                if label:
                    content_gaps.append(label)

    competitor_text = (
        ", ".join(competitor_names)
        if competitor_names
        else "relevant competitors in the market"
    )
    gaps_text = (
        ", ".join(content_gaps)
        if content_gaps
        else "clear buying questions, trust signals, and local relevance"
    )

    return (
        f"Users searching '{target_query}' are likely looking for a clear and trustworthy answer before deciding what to choose. "
        f"This brief should position {client_name or 'the brand'} as a credible option in {industry or 'its category'}"
        f"{' in ' + location if location else ''}. "
        f"Emphasise decision-making factors, practical buying guidance, and what makes the brand different. "
        f"Competitors currently visible include {competitor_text}. "
        f"The content should close gaps around {gaps_text}. "
        f"Brand website: {website}. "
        f"{'Additional brand notes: ' + notes if notes else ''}"
    ).strip()


def extract_domain_from_url(url):
    if not url:
        return ""

    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    return normalize_website(domain)


def discover_competitors_from_web(client):
    api_key = os.getenv("TAVILY_API_KEY")

    if not api_key:
        return []

    tavily_client = TavilyClient(api_key=api_key)

    client_name = client.get("name", "")
    industry = client.get("industry", "")
    location = client.get("location", "")
    own_domain = normalize_website(client.get("website", ""))

    search_queries = [
        f"best {industry} in {location}",
        f"top {industry} companies in {location}",
        f"{industry} services in {location}",
        f"{client_name} competitors",
        f"recommended {industry} provider in {location}",
    ]

    domain_counts = {}

    for query in search_queries:
        try:
            response = tavily_client.search(
                query=query,
                search_depth="basic",
                max_results=8,
            )

            for result in response.get("results", []):
                domain = extract_domain_from_url(result.get("url", ""))

                if not domain:
                    continue

                if own_domain and domain == own_domain:
                    continue

                if (
                    "google." in domain
                    or "facebook." in domain
                    or "linkedin." in domain
                ):
                    continue

                if domain not in domain_counts:
                    domain_counts[domain] = {
                        "name": domain,
                        "mentions": 0,
                        "sources": [],
                    }

                domain_counts[domain]["mentions"] += 1
                domain_counts[domain]["sources"].append(query)

        except Exception as e:
            print("Tavily competitor discovery error:", e)

    results = sorted(
        domain_counts.values(), key=lambda x: x["mentions"], reverse=True
    )

    return results[:10]


def get_prompt_visibility(client_id, target_query):
    rows = PromptTracking.query.filter_by(user_id=current_user.id).all()

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
    rows = PromptTracking.query.filter_by(user_id=current_user.id).all()

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
        (visibility * 0.4)
        + ((100 - competitors) * 0.3)
        + (content_score * 0.3)
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
        "visibility": visibility,
    }


def auto_detect_competitors_for_client(client_id, user_id):
    client = get_client_by_id(client_id)

    if not client:
        return []

    competitors = {}

    def add_competitor(name, source="audit"):
        name = safe_str(name)
        if not name or name in ["—", "-", "None", "N/A"]:
            return

        key = (
            name.lower()
            .replace("https://", "")
            .replace("http://", "")
            .replace("www.", "")
            .strip("/")
        )

        if not key:
            return

        if key not in competitors:
            competitors[key] = {
                "name": key,
                "mentions": 0,
                "sources": set(),
            }

        competitors[key]["mentions"] += 1
        competitors[key]["sources"].add(source)

    # 1) Pull from saved audits
    audits = get_saved_audits(user_id=user_id)

    matched_audits = [
        audit
        for audit in audits
        if str(audit.get("client_id")) == str(client_id)
        or normalize_website(audit.get("website", ""))
        == normalize_website(client.get("website", ""))
    ]

    for audit in matched_audits:
        for comp in audit.get("top_competitors", []) or []:
            if isinstance(comp, dict):
                add_competitor(comp.get("name") or comp.get("domain"), "audit")
            else:
                add_competitor(comp, "audit")

        full_data = read_full_audit_data(audit.get("filename"))
        if full_data:
            for row in full_data.get("ai_answer_results", []) or []:
                for comp in row.get("competitors_mentioned", []) or []:
                    add_competitor(comp, "ai_answer")

                for comp in row.get("latest_competitors", []) or []:
                    add_competitor(comp, "ai_answer")

    # 2) Pull from prompt tracking
    domain = normalize_website(client.get("website", ""))

    prompt_rows = PromptTracking.query.filter_by(user_id=user_id).all()

    for row in prompt_rows:
        if domain and row.domain and normalize_website(row.domain) != domain:
            continue

        add_competitor(row.top_competitor, "prompt_tracking")

    results = []

    for item in competitors.values():
        results.append(
            {
                "name": item["name"],
                "mentions": item["mentions"],
                "sources": sorted(list(item["sources"])),
            }
        )

    results = sorted(results, key=lambda x: x["mentions"], reverse=True)

    return results[:10]


def demo_website_page_json(client_name="Demo Business"):
    return {
        "page_type": "service",
        "title": f"{client_name} AI Visibility Service Page",
        "slug": "ai-visibility-service-page",
        "seo": {
            "title": f"{client_name} | AI Visibility Services",
            "description": f"Improve how {client_name} appears in AI search answers, ChatGPT recommendations, and customer discovery prompts.",
        },
        "sections": [
            {
                "type": "hero",
                "eyebrow": "AI Visibility Growth",
                "headline": f"Help {client_name} get discovered in AI answers",
                "subtext": "We identify why your business is missing from AI-generated recommendations and create the content needed to improve visibility.",
                "primary_cta": "Request an AI Visibility Audit",
                "secondary_cta": "View Recommendations",
            },
            {
                "type": "problem",
                "headline": "Your customers are searching differently now",
                "body": "More customers are asking AI tools for recommendations before they visit Google or compare websites. If your business is not mentioned, you may be invisible at the decision stage.",
            },
            {
                "type": "services",
                "headline": "What we improve",
                "items": [
                    {
                        "title": "AI Answer Visibility",
                        "description": "Track whether your brand appears in relevant AI-generated answers.",
                    },
                    {
                        "title": "Content Gap Fixes",
                        "description": "Find missing FAQs, service pages, comparison content, and trust signals.",
                    },
                    {
                        "title": "AEO-Ready Pages",
                        "description": "Generate structured pages designed for AI answer engines and human readers.",
                    },
                ],
            },
            {
                "type": "proof",
                "headline": "Built for measurable improvement",
                "items": [
                    "Track prompt visibility over time",
                    "Compare against competitors",
                    "Generate pages from missed opportunities",
                    "Review and publish updates",
                ],
            },
            {
                "type": "faq",
                "headline": "Frequently asked questions",
                "items": [
                    {
                        "question": "What is AI visibility?",
                        "answer": "AI visibility means how often your business appears when users ask AI tools for recommendations, comparisons, or service providers.",
                    },
                    {
                        "question": "How is this different from SEO?",
                        "answer": "SEO focuses on search engine rankings. AEO focuses on being included and recommended inside AI-generated answers.",
                    },
                    {
                        "question": "Can this improve my website?",
                        "answer": "Yes. The system identifies missing content and turns it into website-ready sections or pages.",
                    },
                ],
            },
            {
                "type": "cta",
                "headline": "Ready to improve your AI visibility?",
                "body": "Start with an audit and see where your business is missing from AI answers.",
                "button": "Start Audit",
            },
        ],
    }


@app.route("/client/<client_id>/website-builder")
@login_required
def website_builder_page(client_id):
    if current_user.plan == "free":
        flash(
            "Website and page generation is available on Pro and Growth plans.",
            "info",
        )
        return redirect(url_for("pricing_page"))
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    row = Client.query.filter_by(
        slug=str(client_id), user_id=current_user.id
    ).first()
    if not row and str(client_id).isdigit():
        row = Client.query.filter_by(
            id=int(client_id), user_id=current_user.id
        ).first()

    projects = []
    if row:
        projects = (
            GeneratedWebsiteProject.query.filter_by(
                user_id=current_user.id, client_id=row.id
            )
            .order_by(GeneratedWebsiteProject.created_at.desc())
            .all()
        )

    return render_template(
        "website_builder.html", client=client, projects=projects
    )


@app.route("/client/<client_id>/competitors/auto-detect")
@login_required
def auto_detect_competitors_page(client_id):
    if current_user.plan == "free":
        flash(
            "Automatic competitor discovery is available on Pro and Growth plans.",
            "info",
        )
        return redirect(url_for("pricing_page"))
    client = get_client_by_id(client_id)

    if not client:
        abort(404)

    detected_competitors = auto_detect_competitors_for_client(
        client_id=client_id, user_id=current_user.id
    )

    if not detected_competitors:
        flash(
            "No competitors found yet. Run an audit or add prompt tracking first, then try auto-detect again.",
            "warning",
        )
        return redirect(
            url_for(
                "client_competitors_page",
                client_id=client_id,
                domain=normalize_website(client.get("website", "")),
            )
        )

    competitor_args = {}

    for index, comp in enumerate(detected_competitors[:4], start=1):
        competitor_args[f"competitor_{index}"] = comp["name"]

    flash(
        f"Auto-filled {len(competitor_args)} competitors from audit/prompt data.",
        "success",
    )

    return redirect(
        url_for(
            "client_competitors_page",
            client_id=client_id,
            domain=normalize_website(client.get("website", "")),
            **competitor_args,
        )
    )


@app.route("/client/<client_id>/website-builder/generate", methods=["POST"])
@login_required
def generate_full_website(client_id):
    if current_user.plan == "free":
        flash(
            "Website and page generation is available on Pro and Growth plans.",
            "info",
        )
        return redirect(url_for("pricing_page"))

    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    row = Client.query.filter_by(
        slug=str(client_id), user_id=current_user.id
    ).first()

    if not row and str(client_id).isdigit():
        row = Client.query.filter_by(
            id=int(client_id), user_id=current_user.id
        ).first()

    if not row:
        abort(404)

    # Step 1: Build brand kit / blueprint first
    blueprint = build_demo_website_blueprint(client)

    # Save temporary brand kit in session for preview/approval
    session["pending_website_blueprint"] = blueprint
    session["pending_website_client_id"] = row.id
    session["pending_website_client_slug"] = row.slug

    flash("Brand kit generated. Please review before creating the website.", "success")
    return redirect(url_for("preview_website_brand_kit", client_id=row.slug))

@app.route("/client/<client_id>/website-builder/brand-kit")
@login_required
def preview_website_brand_kit(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    blueprint = session.get("pending_website_blueprint")

    if not blueprint:
        flash("No brand kit found. Please generate a website draft first.", "warning")
        return redirect(url_for("website_builder_page", client_id=client_id))

    return render_template(
        "website_brand_kit_preview.html",
        client=client,
        blueprint=blueprint,
    )

@app.route("/client/<client_id>/website-builder/approve-brand-kit", methods=["POST"])
@login_required
def approve_website_brand_kit(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    row = Client.query.filter_by(
        slug=str(client_id), user_id=current_user.id
    ).first()

    if not row and str(client_id).isdigit():
        row = Client.query.filter_by(
            id=int(client_id), user_id=current_user.id
        ).first()

    if not row:
        abort(404)

    blueprint = session.get("pending_website_blueprint")

    if not blueprint:
        flash("Brand kit expired. Please generate it again.", "warning")
        return redirect(url_for("website_builder_page", client_id=client_id))

    blueprint = apply_brand_kit_form_edits(blueprint, request.form)

    project = GeneratedWebsiteProject(
        user_id=current_user.id,
        client_id=row.id,
        title=f"{client.get('name')} Website Revamp",
        theme=blueprint["theme"],
        status="draft",
        blueprint_json=blueprint,
    )

    db.session.add(project)
    db.session.flush()

    for page_config in blueprint["pages"]:
        page_json = build_generated_site_page(client, blueprint, page_config)

        page = GeneratedWebsitePage(
            project_id=project.id,
            user_id=current_user.id,
            client_id=row.id,
            title=page_json["title"],
            slug=page_json["slug"],
            page_type=page_json["page_type"],
            status="draft",
            page_json=page_json,
        )
        db.session.add(page)

    db.session.commit()

    session.pop("pending_website_blueprint", None)
    session.pop("pending_website_client_id", None)
    session.pop("pending_website_client_slug", None)

    flash("Website draft generated from approved brand kit.", "success")
    return redirect(url_for("preview_website_project", project_id=project.id))


def apply_brand_kit_form_edits(blueprint, form):
    edited = dict(blueprint or {})

    text_fields = [
        "client_name",
        "style_direction",
        "primary_cta",
        "secondary_cta",
        "business_type",
        "visual_style",
    ]
    color_fields = [
        "primary_color",
        "secondary_color",
        "accent_color",
        "text_color",
    ]

    for field in text_fields:
        value = (form.get(field) or "").strip()
        if value:
            edited[field] = value

    for field in color_fields:
        value = (form.get(field) or "").strip()
        if _is_hex_color(value):
            edited[field] = value

    personality = _split_lines_or_commas(form.get("personality", ""))
    if personality:
        edited["personality"] = personality[:6]

    aeo_focus = _split_lines_or_commas(form.get("aeo_focus", ""))
    if aeo_focus:
        edited["aeo_focus"] = aeo_focus[:8]

    pages = []
    for index, page in enumerate(edited.get("pages") or []):
        page_copy = dict(page)
        title = (form.get(f"page_title_{index}") or "").strip()
        slug = (form.get(f"page_slug_{index}") or "").strip()
        goal = (form.get(f"page_goal_{index}") or "").strip()

        if title:
            page_copy["title"] = title
        if slug:
            page_copy["slug"] = slugify(slug) or page_copy.get("slug")
        if goal:
            page_copy["goal"] = goal

        pages.append(page_copy)

    if pages:
        edited["pages"] = pages

    return edited


def _split_lines_or_commas(value):
    raw_items = str(value or "").replace("\n", ",").split(",")
    return [item.strip() for item in raw_items if item.strip()]


def _is_hex_color(value):
    value = str(value or "").strip()
    if len(value) != 7 or not value.startswith("#"):
        return False
    return all(char in "0123456789abcdefABCDEF" for char in value[1:])


@app.route("/website-builder/project/<int:project_id>/preview")
@login_required
def preview_website_project(project_id):
    project = GeneratedWebsiteProject.query.get_or_404(project_id)

    if project.user_id != current_user.id:
        abort(403)

    pages = (
        GeneratedWebsitePage.query.filter_by(
            project_id=project.id, user_id=current_user.id
        )
        .order_by(GeneratedWebsitePage.id.asc())
        .all()
    )

    webflow_status = get_webflow_setup_status()
    post_publish_tracking = build_post_publish_tracking(project)

    return render_template(
        "website_project_preview.html",
        project=project,
        pages=pages,
        blueprint=project.blueprint_json,
        webflow_enabled=is_webflow_configured(),
        webflow_status=webflow_status,
        post_publish_tracking=post_publish_tracking,
        impact_report=build_post_publish_impact_report(project),
        mvp_readiness=build_website_mvp_readiness(
            project,
            pages,
            webflow_status,
        ),
    )


@app.route(
    "/website-builder/project/<int:project_id>/publish", methods=["POST"]
)
@login_required
def publish_website_project(project_id):
    project = GeneratedWebsiteProject.query.get_or_404(project_id)

    if project.user_id != current_user.id:
        abort(403)

    project.status = "published"

    pages = GeneratedWebsitePage.query.filter_by(project_id=project.id).all()
    for page in pages:
        page.status = "published"

    db.session.commit()

    flash("Website published.", "success")
    return redirect(url_for("public_website_project", project_id=project.id))


@app.route(
    "/website-builder/project/<int:project_id>/verify-webflow",
    methods=["POST"],
)
@login_required
def verify_website_project_webflow(project_id):
    project = GeneratedWebsiteProject.query.get_or_404(project_id)

    if project.user_id != current_user.id:
        abort(403)

    try:
        result = verify_webflow_connection()
    except WebflowConfigError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("preview_website_project", project_id=project.id))
    except WebflowAPIError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("preview_website_project", project_id=project.id))

    blueprint = project.blueprint_json or {}
    blueprint["webflow_connection"] = {
        "verified_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "collection_name": result["collection_name"],
        "site_id": result["site_id"],
        "collection_id": result["collection_id"],
        "schema": result.get("schema"),
    }
    project.blueprint_json = blueprint
    flag_modified(project, "blueprint_json")
    db.session.commit()

    flash(
        f"Publishing setup verified: {result['collection_name']} collection is ready.",
        "success",
    )
    return redirect(url_for("preview_website_project", project_id=project.id))


def build_website_mvp_readiness(project, pages, webflow_status):
    blueprint = project.blueprint_json or {}
    generated_pages_ready = bool(pages) and all(
        (page.page_json or {}).get("sections") for page in pages
    )
    exported_pages = [
        page
        for page in pages
        if ((page.page_json or {}).get("webflow") or {}).get("item_id")
    ]
    webflow_connection = blueprint.get("webflow_connection") or {}
    webflow_schema = (
        webflow_connection.get("schema")
        or (blueprint.get("webflow_export") or {}).get("schema")
        or {}
    )
    post_publish_tracking = build_post_publish_tracking(project)

    checks = [
        {
            "label": "Brand kit approved",
            "done": bool(blueprint),
            "detail": "Website generation starts from an approved brand and AEO blueprint.",
        },
        {
            "label": "Pages generated",
            "done": generated_pages_ready,
            "detail": f"{len(pages)} generated page(s) ready for review.",
        },
        {
            "label": "Publishing configured",
            "done": webflow_status.get("configured", False),
            "detail": "API token, site ID, and collection ID are set.",
        },
        {
            "label": "Publishing verified",
            "done": bool(webflow_connection),
            "detail": webflow_connection.get("collection_name")
            or "Connection has not been verified yet.",
        },
        {
            "label": "CMS schema valid",
            "done": webflow_schema.get("valid", False),
            "detail": "All mapped CMS fields exist."
            if webflow_schema.get("valid")
            else "Verify publishing setup to check required fields.",
        },
        {
            "label": "Pages synced",
            "done": bool(pages) and len(exported_pages) == len(pages),
            "detail": f"{len(exported_pages)} of {len(pages)} page(s) synced to the publishing layer.",
        },
        {
            "label": "Publication ready",
            "done": project.status == "published"
            or (
                bool(pages)
                and len(exported_pages) == len(pages)
                and webflow_schema.get("valid", False)
            ),
            "detail": "Ready for final publish once pages are synced and reviewed.",
        },
        {
            "label": "Track and improve loop",
            "done": post_publish_tracking.get("started", False)
            or post_publish_tracking.get("re_audited_after_export", False),
            "detail": post_publish_tracking.get("summary"),
        },
    ]

    completed = sum(1 for check in checks if check["done"])
    return {
        "percent": round((completed / len(checks)) * 100),
        "completed": completed,
        "total": len(checks),
        "checks": checks,
    }


def build_post_publish_tracking(project):
    blueprint = project.blueprint_json or {}
    export_data = blueprint.get("webflow_export") or {}
    tracking_data = blueprint.get("post_publish_tracking") or {}
    exported_at = export_data.get("exported_at")

    client = Client.query.filter_by(
        id=project.client_id,
        user_id=project.user_id,
    ).first()

    client_route_id = client.slug if client else project.client_id
    matched_audits = _get_project_saved_audits(project, client)
    latest_audit = matched_audits[0] if matched_audits else None

    latest_audit_at = latest_audit.get("saved_at") if latest_audit else None
    re_audited_after_export = _iso_after(latest_audit_at, exported_at)

    if re_audited_after_export:
        status = "measured"
        summary = "A post-publish audit has been captured. Review the growth plan for improvement actions."
    elif tracking_data:
        status = tracking_data.get("status", "waiting_for_reaudit")
        summary = "Tracking cycle started. Run a fresh audit after publishing to measure impact."
    elif exported_at:
        status = "ready_to_track"
        summary = "Pages are synced. Start tracking, then re-audit after publishing."
    else:
        status = "not_ready"
        summary = "Sync pages to the publishing layer before starting the post-publish tracking loop."

    return {
        "status": status,
        "summary": summary,
        "started": bool(tracking_data),
        "started_at": tracking_data.get("started_at"),
        "exported_at": exported_at,
        "latest_audit": latest_audit,
        "latest_audit_at": latest_audit_at,
        "re_audited_after_export": re_audited_after_export,
        "client_route_id": client_route_id,
    }


def build_post_publish_impact_report(project):
    blueprint = project.blueprint_json or {}
    exported_at = (blueprint.get("webflow_export") or {}).get("exported_at")
    client = Client.query.filter_by(
        id=project.client_id,
        user_id=project.user_id,
    ).first()
    matched_audits = _get_project_saved_audits(project, client)

    before_audits = [
        audit
        for audit in matched_audits
        if not exported_at or not _iso_after(audit.get("saved_at"), exported_at)
    ]
    after_audits = [
        audit
        for audit in matched_audits
        if exported_at and _iso_after(audit.get("saved_at"), exported_at)
    ]

    before = before_audits[0] if before_audits else None
    after = after_audits[0] if after_audits else None
    comparison = compare_audits(after, before) if before and after else None

    score_rows = [
        _impact_score_row("Overall", "normalized_score", before, after),
        _impact_score_row("Visibility", "visibility_score", before, after),
        _impact_score_row("Content", "content_score", before, after),
        _impact_score_row("Schema", "schema_score", before, after),
    ]

    if after:
        status = "measured"
        summary = "Post-publish impact has been measured against the latest pre-sync audit."
    elif exported_at:
        status = "waiting_for_reaudit"
        summary = "Run a fresh audit after publishing to measure impact."
    else:
        status = "not_ready"
        summary = "Sync and publish pages before measuring impact."

    recommendations = []
    if after:
        recommendations = (
            after.get("top_recommendations")
            or after.get("top_content_gaps")
            or []
        )[:5]

    return {
        "status": status,
        "summary": summary,
        "exported_at": exported_at,
        "before": before,
        "after": after,
        "comparison": comparison,
        "score_rows": score_rows,
        "recommendations": recommendations,
    }


def _get_project_saved_audits(project, client=None):
    matched_audits = [
        audit
        for audit in get_saved_audits(user_id=project.user_id)
        if _audit_matches_project(audit, project, client)
    ]
    return sort_audits(matched_audits, sort_by="saved_at", order="desc")


def _audit_matches_project(audit, project, client=None):
    audit_client_id = str(audit.get("client_id") or "")
    project_client_id = str(project.client_id)
    client_slug = str(getattr(client, "slug", "") or "")

    if audit_client_id in {project_client_id, client_slug}:
        return True

    audit_website = normalize_website(audit.get("website", ""))
    client_website = normalize_website(getattr(client, "website", "") or "")
    return bool(audit_website and client_website and audit_website == client_website)


def _impact_score_row(label, key, before, after):
    before_value = before.get(key, 0) if before else None
    after_value = after.get(key, 0) if after else None
    delta = None
    if before_value is not None and after_value is not None:
        delta = round((after_value or 0) - (before_value or 0), 2)

    return {
        "label": label,
        "before": before_value,
        "after": after_value,
        "delta": delta,
        "direction": "up" if delta and delta > 0 else "down" if delta and delta < 0 else "flat",
    }


def _iso_after(candidate, baseline):
    if not candidate or not baseline:
        return False
    return str(candidate) > str(baseline)


@app.route(
    "/website-builder/project/<int:project_id>/start-tracking",
    methods=["POST"],
)
@login_required
def start_website_project_tracking(project_id):
    project = GeneratedWebsiteProject.query.get_or_404(project_id)

    if project.user_id != current_user.id:
        abort(403)

    tracking = build_post_publish_tracking(project)
    if not tracking.get("exported_at"):
        flash("Sync the website draft before starting tracking.", "warning")
        return redirect(url_for("preview_website_project", project_id=project.id))

    blueprint = project.blueprint_json or {}
    blueprint["post_publish_tracking"] = {
        "status": "waiting_for_reaudit",
        "started_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "exported_at": tracking.get("exported_at"),
    }
    project.blueprint_json = blueprint
    flag_modified(project, "blueprint_json")

    client = Client.query.filter_by(
        id=project.client_id,
        user_id=current_user.id,
    ).first()
    client_name = client.name if client else "Workspace"
    target_query = f"Post-publish improvement review for {project.title}"
    existing_items = get_queue_items(
        client_id=str(project.client_id),
        user_id=current_user.id,
    )
    existing_queries = {
        (item.get("target_query") or "").strip().lower()
        for item in existing_items
    }

    if target_query.lower() not in existing_queries:
        add_queue_item(
            client_id=str(project.client_id),
            client_name=client_name,
            target_query=target_query,
            content_type="post_publish_review",
            item_type="brief",
            title="Review post-publish AEO impact",
            content=(
                "Run a fresh audit after the published pages are live, compare "
                "visibility and content scores, then turn the highest-impact "
                "findings into improvement tasks."
            ),
            status="pending",
            priority="high",
            source="manual",
            credits_required=0,
            execution_type="human_review",
            source_action_title="Post-publish tracking",
            user_id=current_user.id,
        )

    db.session.commit()
    flash("Post-publish tracking started. Run a fresh audit after publishing.", "success")
    return redirect(url_for("preview_website_project", project_id=project.id))


@app.route(
    "/website-builder/project/<int:project_id>/export-webflow",
    methods=["POST"],
)
@login_required
def export_website_project_to_webflow(project_id):
    project = GeneratedWebsiteProject.query.get_or_404(project_id)

    if project.user_id != current_user.id:
        abort(403)

    pages = (
        GeneratedWebsitePage.query.filter_by(
            project_id=project.id, user_id=current_user.id
        )
        .order_by(GeneratedWebsitePage.id.asc())
        .all()
    )

    # Prefer the legacy single-collection export if WEBFLOW_COLLECTION_ID is
    # set; otherwise fall through to per-page routing using BLOG / FAQ /
    # SERVICE / LOCATION env vars (matching the per-content-type setup most
    # users have today).
    legacy_collection = os.getenv("WEBFLOW_COLLECTION_ID")
    use_per_collection = (
        not legacy_collection or legacy_collection.startswith("your_")
    )

    if use_per_collection:
        try:
            export_result = _export_website_project_per_collection(project, pages)
        except Exception as exc:
            logger.error(f"Per-collection export failed: {exc}")
            flash(
                "We couldn't sync this website to your CMS. Try again, or "
                "reach out to your admin if the problem keeps happening.",
                "danger",
            )
            return redirect(url_for("preview_website_project", project_id=project.id))
    else:
        try:
            export_result = export_project_to_webflow(project, pages)
        except WebflowConfigError as exc:
            logger.warning(f"Site publishing config issue: {exc}")
            flash(
                "Publishing isn't fully set up on this site yet. "
                "Reach out to your admin to enable it.",
                "warning",
            )
            return redirect(url_for("preview_website_project", project_id=project.id))
        except WebflowAPIError as exc:
            logger.warning(f"Site publishing API error: {exc}")
            flash(
                "We couldn't sync this website to your CMS. Try again, or "
                "reach out to your admin.",
                "danger",
            )
            return redirect(url_for("preview_website_project", project_id=project.id))

    blueprint = project.blueprint_json or {}
    blueprint["webflow_export"] = export_result
    project.blueprint_json = blueprint
    flag_modified(project, "blueprint_json")

    if not use_per_collection:
        # The legacy path stamps webflow data per page outside the function;
        # the per-collection path already stamped during the inner loop.
        exported_by_page_id = {
            item.get("local_page_id"): item for item in export_result.get("pages", [])
        }
        exported_at = export_result.get("exported_at")
        for page in pages:
            item = exported_by_page_id.get(page.id)
            if not item:
                continue
            page_json = page.page_json or {}
            page_json["webflow"] = {
                "item_id": item.get("webflow_item_id"),
                "last_action": item.get("action"),
                "exported_at": exported_at,
                "published": export_result.get("published", False),
                "live_url": item.get("live_url"),
            }
            page.page_json = page_json
            flag_modified(page, "page_json")

    db.session.commit()

    synced_count = len(export_result.get("pages", []))
    skipped = export_result.get("skipped") or []
    errors = export_result.get("errors") or []

    if errors:
        flash(
            f"Synced {synced_count} page(s); {len(errors)} failed. "
            "Check the preview for details.",
            "warning",
        )
    elif skipped:
        flash(
            f"Synced {synced_count} page(s); skipped {len(skipped)} that don't have a CMS collection on this site yet.",
            "success",
        )
    else:
        flash(
            f"Synced {synced_count} page(s) to your CMS as drafts. "
            "Review and go live from your CMS.",
            "success",
        )
    return redirect(url_for("preview_website_project", project_id=project.id))


@app.route("/site/<int:project_id>")
def public_website_project(project_id):
    project = GeneratedWebsiteProject.query.get_or_404(project_id)

    if project.status != "published":
        abort(404)

    page = GeneratedWebsitePage.query.filter_by(
        project_id=project.id, slug="home", status="published"
    ).first()

    if not page:
        abort(404)

    pages = (
        GeneratedWebsitePage.query.filter_by(
            project_id=project.id, status="published"
        )
        .order_by(GeneratedWebsitePage.id.asc())
        .all()
    )

    return render_template(
        "generated_full_site.html",
        project=project,
        page=page,
        pages=pages,
        page_json=page.page_json,
        blueprint=project.blueprint_json,
    )


@app.route("/site/<int:project_id>/<slug>")
def public_website_page(project_id, slug):
    project = GeneratedWebsiteProject.query.get_or_404(project_id)

    if project.status != "published":
        abort(404)

    page = GeneratedWebsitePage.query.filter_by(
        project_id=project.id, slug=slug, status="published"
    ).first_or_404()

    pages = (
        GeneratedWebsitePage.query.filter_by(
            project_id=project.id, status="published"
        )
        .order_by(GeneratedWebsitePage.id.asc())
        .all()
    )

    return render_template(
        "generated_full_site.html",
        project=project,
        page=page,
        pages=pages,
        page_json=page.page_json,
        blueprint=project.blueprint_json,
    )


def build_demo_website_blueprint(client):
    industry = (client.get("industry") or "").lower()
    client_name = client.get("name", "Business")
    location = client.get("location", "Singapore")

    if any(word in industry for word in ["clinic", "health", "wellness", "dental", "medical", "aesthetic"]):
        theme = "clinic_wellness"
        style = "calm, clean, reassuring, health-focused"
        functions = ["appointment booking", "contact form", "FAQ schema"]

    elif any(word in industry for word in ["tuition", "education", "school", "enrichment", "learning"]):
        theme = "education_centre"
        style = "friendly, structured, parent-focused, trustworthy"
        functions = ["enquiry form", "programme cards", "FAQ schema"]

    elif any(word in industry for word in [
        "restaurant", "cafe", "food", "f&b", "ice cream", "dessert",
        "confectionery", "bakery", "beverage"
    ]):
        theme = "restaurant_cafe"
        style = "warm, visual, product-led, lifestyle-focused"
        functions = ["product grid", "menu/product section", "WhatsApp CTA"]

    elif any(word in industry for word in [
        "ecommerce", "e-commerce", "retail", "shop", "online store",
        "merchandise", "products"
    ]):
        theme = "ecommerce_store"
        style = "conversion-focused, product-led, clean, modern"
        functions = ["product grid", "checkout CTA", "FAQ schema"]

    else:
        theme = "professional_services"
        style = "premium, trustworthy, clear, professional"
        functions = ["lead form", "consultation CTA", "FAQ schema"]

    primary_cta = (
        "Shop Now"
        if theme in ["restaurant_cafe", "ecommerce_store"]
        else "Book an Appointment"
        if theme == "clinic_wellness"
        else "Enquire Now"
    )

    secondary_cta = (
        "View Products"
        if theme in ["restaurant_cafe", "ecommerce_store"]
        else "View Services"
    )

    return {
        "client_name": client_name,
        "business_type": client.get("industry", "Professional Services"),
        "location": location,
        "theme": theme,
        "style_direction": style,
        "primary_cta": primary_cta,
        "secondary_cta": secondary_cta,
        "functions": functions,
        "pages": [
            {
                "title": "Home",
                "slug": "home",
                "page_type": "home",
                "goal": "Explain the business clearly and convert visitors into enquiries.",
            },
            {
                "title": "Products" if theme in ["restaurant_cafe", "ecommerce_store"] else "Services",
                "slug": "products" if theme in ["restaurant_cafe", "ecommerce_store"] else "services",
                "page_type": "services",
                "goal": "Show what the business offers and answer buying-intent questions.",
            },
            {
                "title": "About",
                "slug": "about",
                "page_type": "about",
                "goal": "Build trust, credibility, and brand confidence.",
            },
            {
                "title": "FAQ",
                "slug": "faq",
                "page_type": "faq",
                "goal": "Answer common questions clearly for humans and AI answer engines.",
            },
            {
                "title": "Contact",
                "slug": "contact",
                "page_type": "contact",
                "goal": "Make it easy for visitors to enquire.",
            },
        ],
        "aeo_focus": [
            f"best {client.get('industry', 'service provider')} in {location}",
            f"{client_name} products" if theme in ["restaurant_cafe", "ecommerce_store"] else f"{client_name} services",
            f"where to buy {client_name}" if theme in ["restaurant_cafe", "ecommerce_store"] else f"how to choose {client.get('industry', 'a provider')}",
            f"{client.get('industry', 'service')} in {location}",
        ],
    }

def build_generated_site_page(client, blueprint, page_config):
    client_name = blueprint.get("client_name") or getattr(client, "name", "This Business")
    page_type = page_config.get("page_type", "home")

    business_type = blueprint.get("business_type", "Business")
    location = blueprint.get("location") or client.get("location") or "Singapore"
    primary_cta = blueprint.get("primary_cta", "Enquire Now")
    secondary_cta = blueprint.get("secondary_cta", "View Services")

    # Get actual products/services from blueprint
    products_or_services = blueprint.get("products_or_services") or blueprint.get("services") or []
    if isinstance(products_or_services, str):
        products_or_services = [
            item.strip()
            for item in products_or_services.replace("\n", ",").split(",")
            if item.strip()
        ]

    # Get brand elements
    style_direction = blueprint.get("style_direction", "")
    personality = blueprint.get("personality", [])
    aeo_focus = blueprint.get("aeo_focus", [])
    functions = blueprint.get("functions", [])

    business_type_lower = str(business_type).lower()
    is_food_or_ecommerce = any(
        word in business_type_lower
        for word in ["food", "beverage", "ecommerce", "ice cream", "confectionery", "dessert"]
    )

    if is_food_or_ecommerce:
        primary_cta = "Shop Now"
        secondary_cta = "View Products"

    # Build dynamic proof items based on personality and functions
    def build_proof_items():
        proof_items = []

        # Add personality-based items
        if personality:
            for trait in personality[:2]:  # Use first 2 personality traits
                if "trustworthy" in trait.lower():
                    proof_items.append(f"Trusted {business_type} in {location}")
                elif "professional" in trait.lower():
                    proof_items.append(f"Professional {business_type} expertise")
                elif "friendly" in trait.lower():
                    proof_items.append(f"Friendly, approachable service")
                elif "modern" in trait.lower():
                    proof_items.append(f"Modern, up-to-date approach")
                elif "warm" in trait.lower():
                    proof_items.append(f"Warm, welcoming experience")
                elif "clear" in trait.lower():
                    proof_items.append(f"Clear, straightforward communication")
                else:
                    proof_items.append(f"{trait} approach to {business_type}")

        # Add function-based items
        if functions:
            for func in functions[:2]:  # Use first 2 functions
                if "faq" in func.lower():
                    proof_items.append("Clear answers to common questions")
                elif "form" in func.lower():
                    proof_items.append("Easy enquiry and contact options")
                elif "booking" in func.lower():
                    proof_items.append("Simple booking and appointment system")
                elif "grid" in func.lower():
                    proof_items.append("Easy-to-browse product/service options")
                else:
                    proof_items.append(f"{func} for customer convenience")

        # Add location and AEO focus items
        if location:
            proof_items.append(f"Local {business_type} serving {location}")

        if aeo_focus and len(proof_items) < 4:
            for focus in aeo_focus[:2]:
                if "best" in focus.lower():
                    proof_items.append(f"Recognized as {focus}")
                elif "where" in focus.lower():
                    proof_items.append(f"Easy to find and contact")

        # Fallback items if we don't have enough
        fallbacks = [
            f"Clear {business_type} information",
            f"Helpful guidance for customers in {location}",
            "AI-answer-friendly content structure",
            "Simple paths to enquire or buy"
        ]

        while len(proof_items) < 4:
            for fallback in fallbacks:
                if fallback not in proof_items:
                    proof_items.append(fallback)
                    break

        return proof_items[:4]

    if page_type == "home":
        if is_food_or_ecommerce:
            # Use actual products/services or smart fallbacks
            featured_items = products_or_services[:3] if products_or_services else [
                "Ice cream", "Confectionery", "Merchandise"
            ]

            # Build headline based on products and personality
            product_focus = ", ".join([item.lower() for item in featured_items[:2]])
            headline = f"Discover {client_name}'s {product_focus} and sweet treats"

            # Build subtext incorporating style direction
            style_desc = f" {style_direction}" if style_direction else ""
            subtext = f"{client_name} brings customers in {location} a{style_desc} experience for discovering desserts, confectionery, merchandise and nostalgic favourites."

            sections = [
                {
                    "type": "hero",
                    "eyebrow": business_type,
                    "headline": headline,
                    "subtext": subtext,
                    "primary_cta": primary_cta,
                    "secondary_cta": secondary_cta,
                },
                {
                    "type": "services",
                    "headline": "Featured products",
                    "items": [
                        {
                            "title": item,
                            "description": f"Explore {item.lower()} from {client_name}."
                        }
                        for item in featured_items
                    ],
                },
                {
                    "type": "proof",
                    "headline": "Why customers love us",
                    "items": build_proof_items(),
                },
                {
                    "type": "cta",
                    "headline": f"Ready to explore {client_name}?",
                    "body": f"Browse products, discover favourites, and find the next sweet treat to enjoy from {client_name} in {location}.",
                    "button": primary_cta,
                },
            ]
        else:
            # Professional services homepage
            style_desc = f" {style_direction}" if style_direction else " clear and trustworthy"
            headline = f"{client_name} helps customers understand their {business_type.lower()} options clearly"
            subtext = f"A{style_desc} website experience designed to explain the business clearly, build trust, and guide customers to the next step."

            sections = [
                {
                    "type": "hero",
                    "eyebrow": business_type,
                    "headline": headline,
                    "subtext": subtext,
                    "primary_cta": primary_cta,
                    "secondary_cta": secondary_cta,
                },
                {
                    "type": "services",
                    "headline": "What we help with",
                    "items": [
                        {
                            "title": item if item else f"Core {business_type.lower()} service",
                            "description": f"Learn more about {item.lower() if item else 'our services'} from {client_name}.",
                        }
                        for item in (products_or_services[:3] or [
                            f"{business_type} guidance",
                            "Professional support",
                            "Clear decision-making help"
                        ])
                    ],
                },
                {
                    "type": "proof",
                    "headline": "Why customers choose us",
                    "items": build_proof_items(),
                },
                {
                    "type": "cta",
                    "headline": "Ready to take the next step?",
                    "body": f"Get in touch with {client_name} to learn more about our {business_type.lower()} options in {location}.",
                    "button": primary_cta,
                },
            ]

    elif page_type == "services":
        # Use actual products/services or smart fallbacks
        service_items = products_or_services[:3] if products_or_services else (
            ["Ice cream", "Confectionery", "Merchandise"]
            if is_food_or_ecommerce
            else ["Main service", "Specialist support", "Ongoing help"]
        )

        headline = "Popular products" if is_food_or_ecommerce else "Core services"
        eyebrow = "Products" if is_food_or_ecommerce else "Services"
        subtext = "Explore the key products and information customers need before taking the next step." if is_food_or_ecommerce else "Explore the key services, what they include, and how to decide what is right for you."

        sections = [
            {
                "type": "hero",
                "eyebrow": eyebrow,
                "headline": f"{eyebrow} from {client_name}",
                "subtext": subtext,
                "primary_cta": primary_cta,
                "secondary_cta": secondary_cta,
            },
            {
                "type": "services",
                "headline": headline,
                "items": [
                    {
                        "title": item,
                        "description": f"Learn more about {item.lower()} from {client_name} in {location}.",
                    }
                    for item in service_items
                ],
            },
            {
                "type": "faq",
                "headline": "Product questions" if is_food_or_ecommerce else "Service questions",
                "items": build_page_faq_items(
                    client_name,
                    business_type,
                    location,
                    is_food_or_ecommerce,
                    aeo_focus,
                    personality
                ),
            },
        ]

    elif page_type == "about":
        sections = [
            {
                "type": "hero",
                "eyebrow": "About",
                "headline": f"About {client_name}",
                "subtext": page_config.get(
                    "goal",
                    "Build trust, credibility, and brand confidence.",
                ),
                "primary_cta": primary_cta,
                "secondary_cta": secondary_cta,
            },
            {
                "type": "proof",
                "headline": "What makes the brand easy to trust",
                "items": build_proof_items(),
            },
            {
                "type": "cta",
                "headline": f"Get to know {client_name}",
                "body": "Explore the products, story, and next steps customers need before making a decision.",
                "button": secondary_cta,
            },
        ]

    elif page_type == "faq":
        sections = [
            {
                "type": "hero",
                "eyebrow": "FAQ",
                "headline": f"Questions about {client_name}",
                "subtext": page_config.get(
                    "goal",
                    "Answer common questions clearly for humans and AI answer engines.",
                ),
                "primary_cta": primary_cta,
                "secondary_cta": secondary_cta,
            },
            {
                "type": "faq",
                "headline": "Common questions",
                "items": build_page_faq_items(
                    client_name,
                    business_type,
                    location,
                    is_food_or_ecommerce,
                    aeo_focus,
                    personality
                ),
            },
        ]

    elif page_type == "contact":
        sections = [
            {
                "type": "hero",
                "eyebrow": "Contact",
                "headline": f"Contact {client_name}",
                "subtext": page_config.get(
                    "goal",
                    "Make it easy for visitors to enquire.",
                ),
                "primary_cta": primary_cta,
                "secondary_cta": secondary_cta,
            },
            {
                "type": "cta",
                "headline": "Ready to take the next step?",
                "body": f"Contact {client_name} to ask questions, explore options, or continue your buying journey in {location}.",
                "button": primary_cta,
            },
        ]

    else:
        sections = [
            {
                "type": "hero",
                "eyebrow": page_config.get("title", "Page"),
                "headline": f"{page_config.get('title', 'Page')} | {client_name}",
                "subtext": page_config.get("goal", ""),
                "primary_cta": primary_cta,
                "secondary_cta": secondary_cta,
            }
        ]

    page_json = {
        "page_type": page_type,
        "title": f"{client_name} | {page_config.get('title', 'Home')}",
        "slug": page_config.get("slug", "home"),
        "seo": {
            "title": f"{page_config.get('title', 'Home')} | {client_name}",
            "description": page_config.get("goal", ""),
        },
        "sections": sections,
    }

    return enrich_generated_page_json(client, blueprint, page_json)


def build_page_faq_items(client_name, business_type, location, is_food_or_ecommerce, aeo_focus=None, personality=None):
    if aeo_focus is None:
        aeo_focus = []
    if personality is None:
        personality = []

    if is_food_or_ecommerce:
        faq_items = [
            {
                "question": f"What can customers find from {client_name}?",
                "answer": f"Customers can discover products, sweet treats, confectionery, and related information from {client_name}.",
            },
            {
                "question": f"Where is {client_name} based?",
                "answer": f"{client_name} serves customers in {location}.",
            },
            {
                "question": "How should visitors choose what to buy?",
                "answer": "Visitors should compare product details, flavour preferences, gifting needs, and ordering information before choosing.",
            },
        ]

        # Add personality-based FAQ if available
        if personality:
            for trait in personality[:1]:  # Use first personality trait
                if "friendly" in trait.lower():
                    faq_items.append({
                        "question": f"What makes {client_name} different?",
                        "answer": f"{client_name} offers a friendly, approachable experience that makes discovering sweet treats enjoyable.",
                    })
                elif "modern" in trait.lower():
                    faq_items.append({
                        "question": f"What makes {client_name} different?",
                        "answer": f"{client_name} brings a modern approach to traditional sweet treats and confectionery.",
                    })
                elif "warm" in trait.lower():
                    faq_items.append({
                        "question": f"What makes {client_name} different?",
                        "answer": f"{client_name} provides a warm, welcoming experience for customers exploring sweet treats.",
                    })

        return faq_items[:4]

    # Professional services FAQs
    faq_items = [
        {
            "question": f"What does {client_name} help with?",
            "answer": f"{client_name} helps customers understand {business_type} options and take the right next step.",
        },
        {
            "question": f"Where does {client_name} operate?",
            "answer": f"{client_name} serves customers in {location}.",
        },
        {
            "question": "How can visitors get started?",
            "answer": "Visitors can review the page information, compare options, and use the primary call-to-action to enquire.",
        },
    ]

    # Add personality-based FAQ if available
    if personality:
        for trait in personality[:1]:  # Use first personality trait
            if "trustworthy" in trait.lower():
                faq_items.append({
                    "question": f"Why choose {client_name}?",
                    "answer": f"{client_name} is known for being trustworthy and reliable in {business_type} services.",
                })
            elif "professional" in trait.lower():
                faq_items.append({
                    "question": f"Why choose {client_name}?",
                    "answer": f"{client_name} brings professional expertise and clear communication to {business_type} services.",
                })
            elif "clear" in trait.lower():
                faq_items.append({
                    "question": f"Why choose {client_name}?",
                    "answer": f"{client_name} provides clear, straightforward guidance for {business_type} decisions.",
                })

    # Add AEO focus-based FAQ if available
    if aeo_focus:
        for focus in aeo_focus[:1]:  # Use first AEO focus
            if "best" in focus.lower():
                faq_items.append({
                    "question": f"What makes {client_name} stand out?",
                    "answer": f"{client_name} is recognized as {focus} in {location}.",
                })
            elif "where" in focus.lower():
                faq_items.append({
                    "question": f"How can customers find {client_name}?",
                    "answer": f"{client_name} is easy to find and contact in {location}.",
                })

    return faq_items[:4]


def enrich_generated_page_json(client, blueprint, page_json):
    client_name = blueprint.get("client_name") or client.get("name") or "Business"
    location = blueprint.get("location") or client.get("location") or "Singapore"
    business_type = blueprint.get("business_type") or client.get("industry") or "Business"
    page_title = page_json.get("title") or client_name
    page_slug = page_json.get("slug") or "home"
    seo = page_json.get("seo") or {}
    description = seo.get("description") or seo.get("meta_description") or ""

    page_json["semantic_profile"] = {
        "entity_name": client_name,
        "entity_type": business_type,
        "location": location,
        "page_intent": page_json.get("page_type", "page"),
        "aeo_focus": blueprint.get("aeo_focus") or [],
    }
    page_json["schema_json"] = build_generated_page_schema(
        client_name=client_name,
        business_type=business_type,
        location=location,
        page_title=page_title,
        page_slug=page_slug,
        description=description,
        sections=page_json.get("sections") or [],
    )

    return page_json


def build_generated_page_schema(
    client_name,
    business_type,
    location,
    page_title,
    page_slug,
    description,
    sections,
):
    page_url = f"/{page_slug}"
    schema_items = [
        {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": client_name,
            "description": description,
            "areaServed": location,
            "knowsAbout": business_type,
        },
        {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": page_title,
            "url": page_url,
            "description": description,
            "about": {
                "@type": "Thing",
                "name": business_type,
            },
        },
        {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": 1,
                    "name": "Home",
                    "item": "/home",
                },
                {
                    "@type": "ListItem",
                    "position": 2,
                    "name": page_title,
                    "item": page_url,
                },
            ],
        },
    ]

    faq_schema = build_faq_schema_from_sections(sections)
    if faq_schema:
        schema_items.append(faq_schema)

    return schema_items


def build_faq_schema_from_sections(sections):
    faq_items = []
    for section in sections or []:
        if section.get("type") != "faq":
            continue
        for item in section.get("items") or section.get("questions") or []:
            question = item.get("question")
            answer = item.get("answer")
            if question and answer:
                faq_items.append(
                    {
                        "@type": "Question",
                        "name": question,
                        "acceptedAnswer": {
                            "@type": "Answer",
                            "text": answer,
                        },
                    }
                )

    if not faq_items:
        return None

    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": faq_items,
    }


@app.route("/content-queue/<item_id>/generate-page")
@login_required
def generate_page_from_queue(item_id):
    if current_user.plan == "free":
        flash(
            "Website and page generation is available on Pro and Growth plans.",
            "info",
        )
        return redirect(url_for("pricing_page"))
    item = get_queue_item_by_id(item_id, user_id=current_user.id)

    if not item:
        flash("Queue item not found.", "error")
        return redirect(url_for("content_queue_page"))

    client_id = str(item.get("client_id", "")).strip()
    client = get_client_by_id(client_id)

    if not client:
        flash("This queue item is linked to a missing workspace.", "warning")
        return redirect(url_for("content_queue_page"))

    target_query = safe_str(item.get("target_query")) or "Service information"
    content_type = safe_str(item.get("content_type")) or "service_page"
    client_name = client.get("name", "Business")
    industry = client.get("industry", "service provider")
    location = client.get("location", "Singapore")
    brand_context = safe_str(item.get("content") or client.get("notes") or "")

    try:
        page_json = generate_structured_website_page(
            client_name=client_name,
            industry=industry,
            location=location,
            target_query=target_query,
            content_type=content_type,
            brand_context=brand_context,
        )
    except Exception as e:
        flash(f"AI page generation failed: {str(e)}", "error")
        return redirect(url_for("content_queue_page", client_id=client_id))

    page_json["slug"] = (
        page_json.get("slug") or slugify(target_query) or "generated-page"
    )
    page_json["title"] = (
        page_json.get("title") or f"{client_name} | {target_query}"
    )
    page_json["page_type"] = page_json.get("page_type") or content_type
    page_json = enrich_generated_page_json(
        client,
        {
            "client_name": client_name,
            "business_type": industry,
            "location": location,
            "aeo_focus": [target_query],
        },
        page_json,
    )

    page = GeneratedWebsitePage(
        project_id=None,
        user_id=current_user.id,
        client_id=client.get("db_id"),
        title=page_json["title"],
        slug=page_json["slug"],
        page_type=page_json["page_type"],
        status="draft",
        page_json=page_json,
    )

    db.session.add(page)
    db.session.commit()

    flash("AI website page generated successfully.", "success")
    return redirect(url_for("preview_generated_page", page_id=page.id))


@app.route("/client/<client_id>/website-engine/demo")
@login_required
def website_engine_demo(client_id):
    client_data = get_client_by_id(client_id)

    if not client_data:
        abort(404)

    page_json = demo_website_page_json(
        client_data.get("name", "Demo Business")
    )

    page = GeneratedWebsitePage(
        user_id=current_user.id,
        client_id=str(client_data.get("db_id")),
        title=page_json["title"],
        slug=page_json["slug"],
        page_type=page_json["page_type"],
        status="draft",
        page_json=page_json,
    )

    db.session.add(page)
    db.session.commit()

    return redirect(url_for("preview_generated_page", page_id=page.id))


@app.route("/website-engine/page/<int:page_id>/preview")
@login_required
def preview_generated_page(page_id):
    page = GeneratedWebsitePage.query.get_or_404(page_id)

    if page.user_id != current_user.id:
        abort(403)

    return render_template(
        "website_engine_preview.html", page=page, page_json=page.page_json
    )


@app.route("/website-engine/page/<int:page_id>/publish", methods=["POST"])
@login_required
def publish_generated_page(page_id):
    page = GeneratedWebsitePage.query.get_or_404(page_id)

    if page.user_id != current_user.id:
        abort(403)

    page.status = "published"
    db.session.commit()

    flash("Page published successfully.", "success")
    return redirect(
        url_for("public_generated_page", page_id=page.id, slug=page.slug)
    )


@app.route("/p/<int:page_id>/<slug>")
def public_generated_page(page_id, slug):
    page = GeneratedWebsitePage.query.get_or_404(page_id)

    if page.status != "published":
        abort(404)

    return render_template(
        "website_engine_public.html", page=page, page_json=page.page_json
    )


def create_content_opportunities_from_latest_audit(client_id, user_id):
    """
    Creates content queue items from the latest saved audit.

    New flow:
    latest saved audit
    -> full audit data
    -> build_recommended_actions()
    -> build_content_opportunities()
    -> add clean opportunities to content queue

    Falls back safely if the audit does not contain research_pack yet.
    """

    audits = get_saved_audits(user_id=user_id)

    matched = [
        audit
        for audit in audits
        if str(audit.get("client_id")) == str(client_id)
    ]

    matched = sort_audits(matched, sort_by="saved_at", order="desc")
    latest = matched[0] if matched else None

    if not latest:
        return 0

    full_data = read_full_audit_data(latest.get("filename"))
    if not full_data:
        return 0

    client = get_client_by_id(client_id)
    if not client:
        return 0

    research_pack = full_data.get("research_pack") or latest.get(
        "research_pack"
    )

    query_rows = full_data.get("ai_answer_results", []) or []

    query_analysis = []

    for row in query_rows:
        query_analysis.append(
            {
                "query": row.get("query"),
                "brand_mentioned": row.get("brand_mentioned", False),
                "score": row.get("score", 0),
                "score_delta": row.get("score_delta", 0),
                "competitors_mentioned": (
                    row.get("competitors_mentioned")
                    or row.get("latest_competitors")
                    or []
                ),
            }
        )

    actions = build_recommended_actions(
        client_name=client.get("name", ""),
        website=client.get("website", ""),
        scores=full_data.get("scores", latest.get("scores", {})),
        query_analysis=query_analysis,
        competitor_analysis={
            "top_competitors": latest.get("top_competitors", [])
        },
        site_findings={
            "technical_issues": full_data.get("technical_issues", [])
        },
        research_pack=research_pack,
    )

    opportunities = build_content_opportunities(actions)

    if not opportunities:
        return 0

    existing_items = get_queue_items(client_id=client_id, user_id=user_id)

    existing_queries = {
        (item.get("target_query") or "").strip().lower()
        for item in existing_items
    }

    active_limit = get_active_queue_limit(current_user)
    active_count = get_active_queue_count(client_id, user_id)
    available_slots = max(0, active_limit - active_count)

    if available_slots <= 0:
        return 0

    created_count = 0

    for opp in opportunities[:available_slots]:
        target_query = safe_str(opp.get("target_query"))
        if not target_query:
            continue

        if target_query.lower() in existing_queries:
            continue

        add_queue_item(
            client_id=client_id,
            client_name=client.get("name")
            or latest.get("client_name")
            or "Workspace",
            target_query=target_query,
            content_type=opp.get("content_type", "service_page"),
            item_type="brief",
            title=opp.get("title") or f"Brief: {target_query}",
            content=opp.get("reason", ""),
            status="pending",
            priority=opp.get("priority", "medium"),
            source="audit_opportunity",
            credits_required=opp.get("credits_required", 0),
            execution_type=opp.get("execution_type", "ai_executable"),
            source_action_title=opp.get("source_action_title", ""),
            user_id=user_id,
        )

        existing_queries.add(target_query.lower())
        created_count += 1

    return created_count


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
        brand_position = (
            1 if visibility == "High" else 3 if visibility == "Medium" else 5
        )
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
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


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


def pdf_filename(label, fallback="darinsights-report"):
    slug = slugify(label) or fallback
    return f"{slug}.pdf"


def _pdf_escape(text):
    return (
        safe_str(text)
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def _wrap_pdf_line(text, max_chars=92):
    words = safe_str(text).split()
    if not words:
        return [""]

    lines = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            lines.append(current)
        current = word

    if current:
        lines.append(current)

    return lines


def build_simple_pdf(lines):
    wrapped_lines = []
    for line in lines:
        wrapped_lines.extend(_wrap_pdf_line(line))

    pages = []
    page_lines = []
    for line in wrapped_lines:
        page_lines.append(line)
        if len(page_lines) >= 42:
            pages.append(page_lines)
            page_lines = []
    if page_lines:
        pages.append(page_lines)
    if not pages:
        pages = [["DarInsights Report"]]

    objects = []
    pages_kids = []

    def add_object(body):
        objects.append(body)
        return len(objects)

    font_obj = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for page in pages:
        y = 790
        content_lines = ["BT", "/F1 11 Tf", "14 TL"]
        content_lines.append(f"50 {y} Td")

        for index, line in enumerate(page):
            if index > 0:
                content_lines.append("T*")
            content_lines.append(f"({_pdf_escape(line)}) Tj")

        content_lines.append("ET")
        stream = "\n".join(content_lines).encode("utf-8")
        content_obj = add_object(
            f"<< /Length {len(stream)} >>\nstream\n"
            f"{stream.decode('utf-8')}\nendstream"
        )
        page_obj = add_object(
            "<< /Type /Page /Parent 0 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> "
            f"/Contents {content_obj} 0 R >>"
        )
        pages_kids.append(page_obj)

    pages_obj_body = (
        f"<< /Type /Pages /Kids [{' '.join(f'{kid} 0 R' for kid in pages_kids)}] "
        f"/Count {len(pages_kids)} >>"
    )
    pages_obj = add_object(pages_obj_body)

    for page_obj in pages_kids:
        objects[page_obj - 1] = objects[page_obj - 1].replace(
            "/Parent 0 0 R", f"/Parent {pages_obj} 0 R"
        )

    catalog_obj = add_object(f"<< /Type /Catalog /Pages {pages_obj} 0 R >>")

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]

    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n{body}\nendobj\n".encode("utf-8"))

    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("utf-8"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("utf-8"))

    output.extend(
        (
            "trailer\n"
            f"<< /Size {len(objects) + 1} /Root {catalog_obj} 0 R >>\n"
            "startxref\n"
            f"{xref_offset}\n"
            "%%EOF\n"
        ).encode("utf-8")
    )
    return bytes(output)


def render_pdf_response(html, filename, fallback_lines=None):
    pdf_bytes = None

    try:
        from weasyprint import HTML
        pdf_bytes = HTML(
            string=html,
            base_url=request.url_root,
        ).write_pdf()
    except Exception:
        pdf_bytes = build_simple_pdf(
            fallback_lines or ["DarInsights Report", "PDF export generated."]
        )

    response = make_response(pdf_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = (
        f"attachment; filename={filename}"
    )
    return response


def audit_summary_pdf_lines(summary_data, summary_filename, report_date):
    scores = summary_data.get("scores", {}) if summary_data else {}
    summary = summary_data.get("summary", {}) if summary_data else {}

    return [
        "DarInsights Audit Summary",
        f"Report date: {report_date}",
        f"Website: {summary_data.get('website', 'Unknown Website')}",
        f"Client: {summary_data.get('client_name', 'Unassigned')}",
        f"Audit type: {summary_data.get('audit_type', 'Audit')}",
        "",
        f"AEO score: {scores.get('normalized_score', summary_data.get('normalized_score', 'Not measured'))}",
        f"Visibility: {scores.get('visibility_score', summary_data.get('visibility_score', 'Not measured'))}",
        f"Content: {scores.get('content_score', summary_data.get('content_score', 'Not measured'))}",
        f"Schema: {scores.get('schema_score', summary_data.get('schema_score', 'Not measured'))}",
        f"Opportunity level: {summary.get('opportunity_level', summary_data.get('opportunity_level', 'N/A'))}",
        "",
        "Verdict",
        summary.get("verdict", summary_data.get("verdict", "Audit completed.")),
        "",
        "Biggest opportunity",
        summary.get(
            "biggest_opportunity",
            "Improve content coverage for high-intent customer questions.",
        ),
        "",
        "Biggest problem",
        summary.get(
            "biggest_problem",
            "The brand is not appearing strongly enough in important AI answer scenarios.",
        ),
        "",
        "Recommended next actions",
        *[
            f"{index}. {action}"
            for index, action in enumerate(
                summary.get("top_3_actions", []) or [
                    "Improve brand visibility in AI answer scenarios.",
                    "Expand content coverage around high-intent customer questions.",
                    "Create structured FAQ, comparison, and service content.",
                ],
                start=1,
            )
        ],
        "",
        f"Summary file: {summary_filename}",
    ]


def client_audit_pdf_lines(client, latest, recommended_actions, report_date):
    lines = [
        "DarInsights Client Audit Report",
        f"Report date: {report_date}",
        f"Client: {client.get('name', 'Workspace')}",
        f"Website: {client.get('website', 'N/A')}",
        "",
        f"Overall score: {latest.get('normalized_score', 'Not measured')}",
        f"Visibility: {latest.get('visibility_score', 'Not measured')}",
        f"Content: {latest.get('content_score', 'Not measured')}",
        f"Opportunity level: {latest.get('opportunity_level', 'N/A')}",
        "",
        "Recommended next actions",
    ]

    for index, action in enumerate(recommended_actions[:5], start=1):
        if isinstance(action, dict):
            title = action.get("title", "Recommended action")
            detail = action.get("recommended_action") or action.get(
                "recommended_fix", ""
            )
            lines.append(f"{index}. {title}. {detail}")
        else:
            lines.append(f"{index}. {action}")

    if not recommended_actions:
        lines.extend([
            "1. Review visibility and content gaps from the latest audit.",
            "2. Generate a focused content brief for the top missed query.",
            "3. Re-audit after updates are published.",
        ])

    return lines


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
        "logo_filename": client.logo_filename,
        "logo_url": url_for("static", filename=f"uploads/workspace_logos/{
                client.logo_filename}") if client.logo_filename else None,
        "created_at": (
            client.created_at.isoformat() if client.created_at else None
        ),
        "updated_at": (
            client.updated_at.isoformat() if client.updated_at else None
        ),
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
        owner_type=client_data.get("owner_type", "company").strip()
        or "company",
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
    row.owner_type = (
        client_data.get("owner_type", "company").strip() or "company"
    )
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
    if summary_filename.endswith("_full.json"):
        return summary_filename
    if not summary_filename.endswith("_summary.json"):
        return None
    return summary_filename.replace("_summary.json", "_full.json")


def get_matching_summary_filename(audit_filename):
    if audit_filename.endswith("_summary.json"):
        return audit_filename
    if audit_filename.endswith("_full.json"):
        return audit_filename.replace("_full.json", "_summary.json")
    return None


def get_summary_path(summary_filename):
    summary_filename = get_matching_summary_filename(summary_filename)
    if not summary_filename:
        return None
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
    summary_files = sorted(
        [f for f in files if f.endswith("_summary.json")], reverse=True
    )

    audits = []
    for filename in summary_files:
        filepath = os.path.join(OUTPUTS_FOLDER, filename)

        try:
            data = load_json_file(filepath)

            saved_user_id = data.get("user_id")
            if user_id is not None and str(saved_user_id) != str(user_id):
                continue

            website = data.get("website", "N/A")
            audits.append(
                {
                    "filename": filename,
                    "website": website,
                    "website_normalized": normalize_website(website),
                    "client_id": (
                        str(data.get("client_id"))
                        if data.get("client_id") is not None
                        else None
                    ),
                    "client_name": data.get("client_name"),
                    "audit_type": data.get("audit_type", "N/A"),
                    "saved_at": data.get("saved_at", ""),
                    "verdict": data.get("summary", {}).get("verdict", "N/A"),
                    "opportunity_level": data.get("summary", {}).get(
                        "opportunity_level", "N/A"
                    ),
                    "normalized_score": data.get("scores", {}).get(
                        "normalized_score", 0
                    ),
                    "visibility_score": data.get("scores", {}).get(
                        "visibility_score", 0
                    ),
                    "content_score": data.get("scores", {}).get(
                        "content_score", 0
                    ),
                    "schema_score": data.get("scores", {}).get(
                        "schema_score", 0
                    ),
                    "scores": data.get("scores", {}),
                    "summary": data.get("summary", {}),
                    "visibility_snapshot": data.get("visibility_snapshot", {}),
                    "top_competitors": data.get("top_competitors", []),
                    "top_content_gaps": data.get("top_content_gaps", []),
                    "top_recommendations": data.get("top_recommendations", []),
                }
            )
        except Exception as e:
            audits.append(
                {
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
                }
            )

    return audits


def filter_audits(audits, search_term="", audit_type="all"):
    results = audits

    if search_term:
        q = search_term.strip().lower()
        results = [
            audit
            for audit in results
            if q in audit.get("website", "").lower()
            or q in audit.get("verdict", "").lower()
            or q in audit.get("opportunity_level", "").lower()
            or q in (audit.get("client_name") or "").lower()
        ]

    if audit_type and audit_type != "all":
        results = [
            audit
            for audit in results
            if audit.get("audit_type", "").lower() == audit_type.lower()
        ]

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

    normalized_delta = delta(
        latest_audit.get("normalized_score", 0),
        previous_audit.get("normalized_score", 0),
    )
    visibility_delta = delta(
        latest_audit.get("visibility_score", 0),
        previous_audit.get("visibility_score", 0),
    )
    content_delta = delta(
        latest_audit.get("content_score", 0),
        previous_audit.get("content_score", 0),
    )
    schema_delta = delta(
        latest_audit.get("schema_score", 0),
        previous_audit.get("schema_score", 0),
    )

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
        "verdict_changed": latest_audit.get("verdict")
        != previous_audit.get("verdict"),
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
    previous_full = read_full_audit_data(
        previous_summary_audit.get("filename")
    )

    if not latest_full or not previous_full:
        return empty_response

    latest_rows = latest_full.get("ai_answer_results", [])
    previous_rows = previous_full.get("ai_answer_results", [])

    latest_map = {
        row.get("query", ""): row for row in latest_rows if row.get("query")
    }
    previous_map = {
        row.get("query", ""): row for row in previous_rows if row.get("query")
    }

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
            if (
                latest_brand != previous_brand
                or latest_position != previous_position
            ):
                change_type = "changed"
                changed += 1
            else:
                change_type = "unchanged"
                unchanged += 1

        if not latest_brand:
            missed_brand_mentions += 1

        comparisons.append(
            {
                "query": query,
                "latest_brand_mentioned": latest_brand,
                "previous_brand_mentioned": previous_brand,
                "latest_brand_position": latest_position,
                "previous_brand_position": previous_position,
                "latest_score": latest_score,
                "previous_score": previous_score,
                "score_delta": score_delta,
                "change_type": change_type,
                "latest_competitors": latest_row.get(
                    "latest_competitors",
                    latest_row.get("competitors_mentioned", []),
                ),
                "previous_competitors": previous_row.get(
                    "previous_competitors",
                    previous_row.get("competitors_mentioned", []),
                ),
            }
        )

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
        client_id_str = (
            str(client.get("id")) if client.get("id") is not None else ""
        )
        client_website_norm = client.get("website_normalized") or ""

        matched_audits = [
            audit
            for audit in audits
            if (
                (
                    audit.get("client_id")
                    and str(audit.get("client_id")) == client_id_str
                )
                or (
                    not audit.get("client_id")
                    and audit.get("website_normalized") == client_website_norm
                )
            )
        ]

        matched_audits = sort_audits(
            matched_audits, sort_by="saved_at", order="desc"
        )
        latest_audit = matched_audits[0] if matched_audits else None
        previous_audit = matched_audits[1] if len(matched_audits) > 1 else None
        comparison = compare_audits(latest_audit, previous_audit)
        query_comparison = build_query_level_comparison(
            latest_audit, previous_audit
        )
        query_rows = (
            query_comparison.get("rows", []) if query_comparison else []
        )

        recommended_actions = build_recommended_actions(
            client_name=client.get("name", ""),
            website=client.get("website", ""),
            scores=latest_audit.get("scores", {}) if latest_audit else {},
            query_analysis=[
                {
                    "query": row.get("query"),
                    "brand_mentioned": row.get(
                        "latest_brand_mentioned", False
                    ),
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
        client_views.append(
            {
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
            }
        )
    return client_views


def get_client_by_id(client_id):
    print("LOOKUP client_id:", client_id)

    row = Client.query.filter_by(
        slug=str(client_id), user_id=current_user.id
    ).first()

    if not row and str(client_id).isdigit():
        row = Client.query.filter_by(
            id=int(client_id), user_id=current_user.id
        ).first()

    if not row:
        return None

    all_clients = build_client_views()
    full_client = next(
        (c for c in all_clients if c.get("id") == row.slug), None
    )

    if full_client:
        return full_client

    return serialize_client_row(row)


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


def get_active_queue_limit(user):
    """
    Controls how many active content queue items each plan can have.
    Published/archived items do not count.
    """
    if not user:
        return 3

    if user_has_unlimited_credits(user):
        return 999

    plan = getattr(user, "plan", "free") or "free"

    limits = {
        "free": 3,
        "pro": 10,
        "growth": 25,
        "dev_unlimited": 999,
    }

    return limits.get(plan, 3)


def is_active_queue_status(status):
    """
    Active queue items are still being worked on.
    Published/archived/deleted items do not count against the queue limit.
    """
    status = (status or "").strip().lower()

    return status not in [
        "published",
        "archived",
        "deleted",
    ]


def get_active_queue_count(client_id, user_id):
    items = get_queue_items(
        client_id=client_id,
        user_id=user_id,
    )

    return len(
        [item for item in items if is_active_queue_status(item.get("status"))]
    )


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
    is_internal_user = current_user.is_authenticated and (
        current_user.email == "pypteltd@gmail.com"
        or getattr(current_user, "role", "") == "admin"
        or getattr(current_user, "plan", "") == "dev_unlimited"
    )

    if not is_internal_user:
        abort(403)

    if mode not in {"single", "multi", "admin", "auto"}:
        abort(404)

    if mode == "auto":
        session.pop("dev_view_mode", None)
    else:
        session["dev_view_mode"] = mode

    return redirect(request.referrer or url_for("settings_preferences"))


def require_internal_access():
    if not current_user.is_authenticated:
        abort(403)

    if (
        current_user.role == "admin"
        or current_user.plan == "dev_unlimited"
        or current_user.email == "pypteltd@gmail.com"
    ):
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
    referral = Referral.query.filter_by(
        referred_user_id=user.id, status="pending"
    ).first()
    if not referral:
        return False

    referrer = User.query.get(referral.referrer_user_id)
    referred_user = User.query.get(referral.referred_user_id)

    if (
        not referrer
        or not referrer.wallet
        or not referred_user
        or not referred_user.wallet
    ):
        return False

    referrer.wallet.balance += referral.reward_amount_referrer
    db.session.add(
        CreditTransaction(
            user_id=referrer.id,
            type="referral_bonus",
            amount=referral.reward_amount_referrer,
            balance_after=referrer.wallet.balance,
            notes=f"Referral reward for user {referred_user.email}",
        )
    )

    referred_user.wallet.balance += referral.reward_amount_referred
    db.session.add(
        CreditTransaction(
            user_id=referred_user.id,
            type="referral_bonus",
            amount=referral.reward_amount_referred,
            balance_after=referred_user.wallet.balance,
            notes="Referral bonus after qualification",
        )
    )

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


def resolve_next_action_urls(next_action):
    if not next_action:
        return None

    resolved = dict(next_action)

    for key in ["primary", "secondary"]:
        target = resolved.get(key)
        if not target:
            continue

        endpoint = target.get("endpoint")
        params = target.get("params") or {}

        try:
            target["url"] = url_for(endpoint, **params)
        except Exception:
            target["url"] = url_for("index")

    return resolved


def build_resolved_next_action(client=None, queue_items=None, **context):
    wallet_balance = 0
    if current_user.is_authenticated and current_user.wallet:
        wallet_balance = current_user.wallet.balance

    action = build_next_best_action(
        client=client,
        queue_items=queue_items,
        user_plan=getattr(current_user, "plan", "free"),
        credit_balance=wallet_balance,
        **context,
    )
    return resolve_next_action_urls(action)


# =========================
# Routes
# =========================


@app.route("/content-queue/<item_id>/brief")
@login_required
def view_queue_brief(item_id):
    item = get_queue_item_by_id(item_id, user_id=current_user.id)

    if not item:
        flash("Queue item not found.", "error")
        return redirect(url_for("content_queue_page"))

    client_id = str(item.get("client_id", "")).strip()
    client = get_client_by_id(client_id)

    if not client:
        flash("This queue item is linked to a missing workspace.", "warning")
        return redirect(url_for("content_queue_page"))

    saved_brief = item.get("brief") or item.get("content") or ""

    brief = {}

    if saved_brief:
        try:
            brief = json.loads(saved_brief)
        except Exception:
            brief = {
                "target_query": item.get("target_query", ""),
                "content_type": item.get("content_type", "service_page"),
                "brief_text": saved_brief,
            }

    result = {
        "target_query": item.get("target_query", ""),
        "content_type": item.get("content_type", "service_page"),
        "brief": brief.get("brief_text") or brief.get("brief") or saved_brief,
        "brief_text": brief.get("brief_text")
        or brief.get("brief")
        or saved_brief,
        "search_intent": brief.get("search_intent", ""),
        "recommended_angle": brief.get("recommended_angle", ""),
        "primary_keywords": brief.get("primary_keywords", []),
        "suggested_title_ideas": brief.get("suggested_title_ideas", []),
        "meta_title": brief.get("meta_title", ""),
        "meta_description": brief.get("meta_description", ""),
        "outline": brief.get("outline", []),
        "faq_questions": brief.get("faq_questions", []),
    }

    aeo = calculate_aeo_score(
        visibility=30,
        competitors=50,
        content_score=get_content_score(saved_brief),
    )

    return render_template(
        "content_brief_result.html",
        client=client,
        result=result,
        brief=brief,
        aeo=aeo,
        top_competitors=[],
        tracked_prompt_count=0,
        target_query=item.get("target_query", ""),
        content_type=item.get("content_type", "service_page"),
        brand_context=item.get("brand_context", ""),
    )


@app.route("/interest", methods=["POST"])
def collect_interest():
    email = request.form.get("email", "").strip().lower()
    company = request.form.get("company", "").strip()
    use_case = request.form.get("use_case", "").strip()

    if not email:
        return redirect("/?interest=missing#early-access")

    os.makedirs("data", exist_ok=True)

    file_path = "data/interest_waitlist.csv"
    file_exists = os.path.exists(file_path)

    with open(file_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow(["created_at", "email", "company", "use_case"])

        writer.writerow(
            [datetime.utcnow().isoformat(), email, company, use_case]
        )

    return redirect("/?interest=success#early-access")


@app.route("/client/<client_id>/query-ideas")
@login_required
def client_query_ideas(client_id):
    client = get_client_by_id(client_id)

    if not client:
        abort(404)

    service_text = request.args.get("services", "").strip()

    services = [s.strip() for s in service_text.split(",") if s.strip()]

    if not services:
        services = [client.get("industry", "")]

    query_ideas = generate_query_ideas(
        industry=client.get("industry", ""),
        location=client.get("location", ""),
        services=services,
    )

    return render_template(
        "query_ideas.html",
        client=client,
        query_ideas=query_ideas,
        services=", ".join(services),
    )


@app.route("/help")
@login_required
def help_page():
    return render_template("help.html", glossary=HELP_GLOSSARY)


@app.route("/pricing")
@login_required
def pricing_page():
    return render_template("pricing.html")


@app.route("/growth-calendar")
@login_required
def growth_calendar_page():
    requested_client_id = request.args.get("client_id", "").strip()
    clients = build_client_views()
    view_mode = get_view_mode(current_user)
    focused_client = get_focused_client_for_user(current_user)

    selected_client = None
    if requested_client_id:
        selected_client = next(
            (c for c in clients if str(c.get("id")) == str(requested_client_id)),
            None,
        )

    if not selected_client and view_mode == "single" and focused_client:
        selected_client = focused_client

    if not selected_client and clients:
        selected_client = clients[0]

    queue_items = []
    queue_items_for_dedupe = []
    if selected_client:
        # Visible queue items for the calendar cards exclude dismissed.
        queue_items = get_queue_items(
            client_id=selected_client.get("id"),
            user_id=current_user.id,
        )
        # For dedupe we also include dismissed items so previously-dismissed
        # recommendations stay hidden from the calendar.
        queue_items_for_dedupe = get_queue_items(
            client_id=selected_client.get("id"),
            user_id=current_user.id,
            include_dismissed=True,
        )

    plan = weekly_growth_recommendations(
        client=selected_client,
        queue_items=queue_items_for_dedupe or queue_items,
    )

    # The calendar layout shouldn't show dismissed cards as visible cards.
    # weekly_growth_recommendations places queue items on the calendar; strip
    # dismissed ones from the rendered cards (they're only there for dedupe).
    for week in plan.get("weeks", []):
        week["cards"] = [
            c for c in week["cards"]
            if c.get("kind") != "queue" or c.get("status") != "dismissed"
        ]
        week["counts"] = {
            "total": len(week["cards"]),
            "queue": sum(1 for c in week["cards"] if c["kind"] == "queue"),
            "recommended": sum(1 for c in week["cards"] if c["kind"] == "recommendation"),
        }

    return render_template(
        "growth_calendar.html",
        clients=clients,
        selected_client=selected_client,
        focused_client=focused_client,
        view_mode=view_mode,
        plan=plan,
    )


@app.route("/growth-calendar/schedule-recommendation", methods=["POST"])
@login_required
def growth_calendar_schedule_recommendation():
    """Pin a recommended action to a specific week — adds it to the queue
    with scheduled_for set to that week's Monday."""
    client_id = request.form.get("client_id", "").strip()
    title = request.form.get("title", "").strip() or "Visibility action"
    target_query = request.form.get("target_query", "").strip()
    content_type = request.form.get("content_type", "").strip() or "service_page"
    priority = request.form.get("priority", "medium").strip() or "medium"
    scheduled_for = request.form.get("scheduled_for", "").strip()
    credits_required = request.form.get("credits_required", "0").strip() or "0"
    source_action_title = request.form.get("source_action_title", title).strip()

    client = get_client_by_id(client_id) if client_id else None
    if not client:
        flash("Workspace not found.", "error")
        return redirect(url_for("growth_calendar_page"))

    add_queue_item(
        client_id=client.get("id"),
        client_name=client.get("name"),
        target_query=target_query,
        content_type=content_type,
        item_type="brief",
        title=title,
        content="",
        status="pending",
        priority=priority,
        source="audit_opportunity",
        credits_required=int(credits_required) if credits_required.isdigit() else 0,
        execution_type="ai_executable",
        source_action_title=source_action_title,
        scheduled_for=scheduled_for or None,
        user_id=current_user.id,
    )

    flash("Recommendation scheduled to the queue.", "success")
    return redirect(url_for("growth_calendar_page", client_id=client.get("id")))


@app.route("/growth-calendar/dismiss-recommendation", methods=["POST"])
@login_required
def growth_calendar_dismiss_recommendation():
    """Hide a recommendation from the calendar without pinning it.

    Stores a queue item with status='dismissed' so the existing dedupe
    filter (matching on source_action_title / target_query) keeps the
    recommendation out of future renders. The dismissed item is hidden
    from the queue page by default — it's a tombstone, not work.
    """
    client_id = request.form.get("client_id", "").strip()
    title = request.form.get("title", "").strip() or "Visibility action"
    target_query = request.form.get("target_query", "").strip()
    content_type = request.form.get("content_type", "").strip() or "service_page"
    priority = request.form.get("priority", "medium").strip() or "medium"

    client = get_client_by_id(client_id) if client_id else None
    if not client:
        flash("Workspace not found.", "error")
        return redirect(url_for("growth_calendar_page"))

    add_queue_item(
        client_id=client.get("id"),
        client_name=client.get("name"),
        target_query=target_query,
        content_type=content_type,
        item_type="brief",
        title=title,
        content="",
        status="dismissed",
        priority=priority,
        source="audit_opportunity",
        credits_required=0,
        execution_type="ai_executable",
        source_action_title=title,
        scheduled_for=None,
        user_id=current_user.id,
    )

    flash("Recommendation dismissed.", "success")
    return redirect(url_for("growth_calendar_page", client_id=client.get("id")))


@app.route("/content-queue/<item_id>/schedule", methods=["POST"])
@login_required
def reschedule_queue_item(item_id):
    scheduled_for = request.form.get("scheduled_for", "").strip()
    item = update_queue_item_schedule(
        item_id, scheduled_for or None, user_id=current_user.id
    )
    if not item:
        abort(404)

    if scheduled_for:
        flash(f"Item rescheduled to {scheduled_for}.", "success")
    else:
        flash("Schedule cleared.", "success")

    redirect_to = request.form.get("redirect_to", "").strip()
    if redirect_to == "queue":
        return redirect(
            url_for("content_queue_page", client_id=request.form.get("client_id", ""))
        )
    return redirect(
        url_for("growth_calendar_page", client_id=request.form.get("client_id", ""))
    )


@app.route("/")
@app.route("/dashboard")
@login_required
def index():
    all_audits = get_saved_audits(user_id=current_user.id)
    clients = build_client_views()

    search_term = request.args.get("q", "").strip()
    audit_type = request.args.get("type", "all").strip().lower()
    sort_by = request.args.get("sort", "saved_at").strip()
    order = request.args.get("order", "desc").strip().lower()

    audits = filter_audits(
        all_audits, search_term=search_term, audit_type=audit_type
    )
    audits = sort_audits(audits, sort_by=sort_by, order=order)

    view_mode = get_view_mode(current_user)
    focused_client = get_focused_client_for_user(current_user)

    overall_score = 0
    visibility_score = 0
    content_score = 0
    entity_score = 0
    trust_score = 0

    if view_mode == "single" and focused_client:
        latest_audit = focused_client.get("latest_audit") or {}

        overall_score = latest_audit.get("normalized_score", 0) or 0
        visibility_score = latest_audit.get("visibility_score", 0) or 0
        content_score = latest_audit.get("content_score", 0) or 0
        entity_score = latest_audit.get("entity_score", 0) or 0
        trust_score = latest_audit.get("trust_score", 0) or 0

    elif view_mode == "multi":
        client_scores = []
        for client in clients:
            latest_audit = client.get("latest_audit") or {}
            score = latest_audit.get("normalized_score")
            if score is not None:
                client_scores.append(score)

        overall_score = (
            round(sum(client_scores) / len(client_scores), 1)
            if client_scores
            else 0
        )

    total_prompts = sum(
        len(client.get("tracked_prompts", []) or []) for client in clients
    )
    mentioned_count = sum(
        client.get("mentioned_count", 0) or 0 for client in clients
    )
    next_action_client = focused_client or (clients[0] if clients else None)
    next_action_queue_items = []

    if next_action_client:
        next_action_queue_items = get_queue_items(
            client_id=next_action_client.get("id"),
            user_id=current_user.id,
        )

    next_best_action = build_resolved_next_action(
        client=next_action_client,
        queue_items=next_action_queue_items,
        has_clients=bool(clients),
        total_audits=len(all_audits),
        total_prompts=total_prompts,
    )

    return render_template(
        "dashboard.html",
        audits=audits,
        clients=clients,
        view_mode=view_mode,
        focused_client=focused_client,
        overall_score=overall_score,
        visibility_score=visibility_score,
        content_score=content_score,
        entity_score=entity_score,
        trust_score=trust_score,
        total_clients=len(clients),
        total_audits=len(all_audits),
        total_prompts=total_prompts,
        mentioned_count=mentioned_count,
        next_best_action=next_best_action,
        search_term=search_term,
        selected_type=audit_type,
        selected_sort=sort_by,
        selected_order=order,
    )


@app.route("/dev/set-plan/<plan>")
@login_required
def dev_set_plan(plan):
    if (
        current_user.email != "pypteltd@gmail.com"
        and current_user.role != "admin"
    ):
        abort(403)

    allowed_plans = [
        "free",
        "starter",
        "pro",
        "growth",
        "agency",
        "dev_unlimited",
    ]
    if plan not in allowed_plans:
        abort(404)

    current_user.plan = plan
    db.session.commit()

    flash(f"Plan changed to {plan}.", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/client/<client_id>/export-pdf")
@login_required
def export_client_audit_pdf(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    # 🔹 Get latest audit data
    latest = client.get("latest_audit", {})

    # 🔹 Safe fallbacks
    recommended_actions = client.get("recommended_actions", [])
    question_rows = client.get("question_rows", [])
    missing_rows = client.get("missing_rows", [])

    top_action = recommended_actions[0] if recommended_actions else None

    # 🔹 Agency (white-label support)
    agency = {
        "name": getattr(current_user, "agency_name", None) or "Your Agency",
        "logo_url": getattr(current_user, "agency_logo_url", None),
        "website": getattr(current_user, "agency_website", None),
        "tagline": getattr(current_user, "agency_tagline", None)
        or "AI Visibility & Content Strategy",
        "footer_text": getattr(current_user, "agency_footer", None),
        "disclaimer": getattr(current_user, "agency_disclaimer", None),
    }

    # 🔹 Render HTML
    report_date = datetime.utcnow().strftime("%d %b %Y")
    html = render_template(
        "client_audit_pdf.html",
        client=client,
        latest=latest,
        recommended_actions=recommended_actions,
        top_action=top_action,
        question_rows=question_rows,
        missing_rows=missing_rows,
        report_date=report_date,
        agency=agency,
        executive_summary=client.get("executive_summary"),
        competitor_notes=client.get("competitor_notes"),
    )

    filename = pdf_filename(f"{client.get('name', 'report')} audit report")
    return render_pdf_response(
        html,
        filename,
        fallback_lines=client_audit_pdf_lines(
            client, latest, recommended_actions, report_date
        ),
    )


@app.route("/clients")
@login_required
def clients_page():
    view_mode = get_view_mode(current_user)
    focused_client = get_focused_client_for_user(current_user)

    if view_mode == "single" and focused_client:
        return redirect(
            url_for("client_detail", client_id=focused_client["id"])
        )

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
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": "DarInsights Pro Upgrade",
                        },
                        "unit_amount": 2900,  # $29.00
                    },
                    "quantity": 1,
                }
            ],
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
    return redirect(url_for("index"))


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

        updated_client = update_client(
            client_id,
            current_user.id,
            {
                "name": name,
                "website": website,
                "industry": industry,
                "location": location,
                "owner_type": owner_type,
                "notes": notes,
            },
        )

        if not updated_client:
            return render_template(
                "client_form.html",
                error="Unable to update client.",
                form_data=request.form,
                mode="edit",
                client=client,
            )

        flash("Client updated successfully.")
        return redirect(
            url_for("client_detail", client_id=updated_client["id"])
        )

    return render_template(
        "client_form.html",
        error=None,
        form_data=client,
        mode="edit",
        client=client,
    )


@app.route("/client/<client_id>/brand-context", methods=["GET", "POST"])
@login_required
def client_brand_context(client_id):
    row = Client.query.filter_by(
        slug=str(client_id), user_id=current_user.id
    ).first()

    if not row and str(client_id).isdigit():
        row = Client.query.filter_by(
            id=int(client_id), user_id=current_user.id
        ).first()

    if not row:
        abort(404)

    if request.method == "POST":
        audience = safe_str(request.form.get("audience"))
        services = safe_str(request.form.get("services"))
        differentiators = safe_str(request.form.get("differentiators"))
        proof = safe_str(request.form.get("proof"))
        tone = safe_str(request.form.get("tone"))
        locations = safe_str(request.form.get("locations"))
        avoid = safe_str(request.form.get("avoid"))
        extra_notes = safe_str(request.form.get("extra_notes"))

        brand_context = f"""
Target audience:
{audience or "Not specified"}

Main services / products:
{services or "Not specified"}

What makes the brand different:
{differentiators or "Not specified"}

Proof, trust signals, or credentials:
{proof or "Not specified"}

Preferred tone:
{tone or "Clear, helpful, professional"}

Locations served:
{locations or row.location or "Not specified"}

Things to avoid:
{avoid or "Not specified"}

Additional notes:
{extra_notes or "Not specified"}
""".strip()

        row.notes = brand_context
        db.session.commit()

        flash(
            "Brand context saved. Future briefs and drafts will use this workspace context.",
            "success",
        )
        return redirect(url_for("client_query_ideas", client_id=row.slug))

    client = serialize_client_row(row)

    return render_template(
        "brand_context_form.html",
        client=client,
        existing_context=row.notes or "",
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
    queue_items = get_queue_items(
        client_id=client.get("id"), user_id=current_user.id
    )
    next_best_action = build_resolved_next_action(
        client=client,
        queue_items=queue_items,
        has_clients=True,
        total_audits=client.get("audit_count", 0),
        total_prompts=len(client.get("tracked_prompts", []) or []),
    )
    return render_template(
        "client_detail.html",
        client=client,
        next_best_action=next_best_action,
    )


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
            return render_template(
                "signup.html", error="All fields are required."
            )

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            return render_template(
                "signup.html", error="Email already registered."
            )

        referrer = None
        if referral_code:
            referrer = User.query.filter_by(
                referral_code=referral_code
            ).first()

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
            return render_template(
                "login.html", error="Invalid email or password."
            )

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

    view_mode = get_view_mode(current_user)
    clients = build_client_views()

    if request.method == "POST":
        website = request.form.get("website", "").strip()
        industry = request.form.get("industry", "").strip()
        location = request.form.get("location", "").strip()
        topic = request.form.get("topic", "").strip()
        audit_type = request.form.get("audit_type", "quick").strip()

        if not website or not industry or not location:
            return render_template(
                "new_audit.html",
                client=client,
                clients=clients,
                preselected_client_id=str(client["id"]),
                form_data={
                    "client_id": str(client["id"]),
                    "client_name": client.get("name", ""),
                    "website": website,
                    "industry": industry,
                    "location": location,
                    "topic": topic,
                    "audit_type": audit_type,
                },
                error="Website, industry, and location are required.",
                view_mode=view_mode,
            )

        if not has_enough_credits(current_user, 1):
            flash(
                "You don’t have enough credits to run another audit.",
                "warning",
            )
            return redirect(url_for("pricing_page"))

        if not spend_credits(current_user, 1, notes="Client audit run"):
            flash("Unable to deduct credits for audit.", "warning")
            return redirect(url_for("pricing_page"))

        try:
            run_audit_for_input(
                website=website,
                industry=industry,
                location=location,
                topic=topic or industry or None,
                audit_type=audit_type,
                client_id=client_id,
                client_name=client.get("name"),
                user_id=current_user.id,
            )

            created_count = create_content_opportunities_from_latest_audit(
                client_id=client_id,
                user_id=current_user.id,
            )

            if created_count > 0:
                flash(
                    f"Audit completed successfully. {created_count} content opportunities added to the queue.",
                    "success",
                )
            else:
                flash(
                    "Audit completed successfully. No new content opportunities were added.",
                    "success",
                )

            return redirect(url_for("client_detail", client_id=client_id))
        except Exception as e:
            refund_credits(
                current_user, 1, notes="Refund for failed client audit"
            )
            flash(f"Audit failed: {str(e)}", "error")
            return redirect(url_for("client_detail", client_id=client_id))

    form_data = {
        "client_id": str(client["id"]),
        "client_name": client.get("name", ""),
        "website": client.get("website", ""),
        "industry": client.get("industry", ""),
        "location": client.get("location", ""),
        "topic": client.get("industry", ""),
        "audit_type": "quick",
    }

    return render_template(
        "new_audit.html",
        client=client,
        clients=clients,
        preselected_client_id=str(client["id"]),
        form_data=form_data,
        error=None,
        view_mode=view_mode,
    )


@app.route("/generate-content/<int:prompt_id>")
@login_required
def generate_content_from_prompt(prompt_id):
    row = PromptTracking.query.filter_by(
        id=prompt_id, user_id=current_user.id
    ).first()

    if not row:
        flash("Prompt not found.", "warning")
        return redirect(url_for("prompt_management_page"))

    return redirect(
        url_for(
            "content_queue_page",
            query=row.prompt,
            topic=row.topic,
            source="prompt_tracking",
        )
    )


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
        content_type = (
            safe_str(request.form.get("content_type", "service_page"))
            or "service_page"
        )
        brand_context = safe_str(request.form.get("brand_context"))

        if not target_query:
            return render_template(
                "content_brief_form.html",
                client=client,
                error="Target query is required.",
                form_data=request.form,
            )

        if not has_enough_credits(current_user, 1):
            flash(
                "You don’t have enough credits to generate another brief.",
                "warning",
            )
            return redirect(url_for("pricing_page"))

        if not spend_credits(
            current_user, 1, notes="Content brief generation"
        ):
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

            tracked_rows = (
                PromptTracking.query.filter_by(user_id=current_user.id)
                .filter(PromptTracking.prompt.ilike(f"%{target_query}%"))
                .all()
            )

            top_competitors = list(
                set(
                    [
                        row.top_competitor
                        for row in tracked_rows
                        if row.top_competitor and row.top_competitor != "—"
                    ]
                )
            )[:3]

            if tracked_rows:
                total_rows = len(tracked_rows)

                visible_rows = [
                    row
                    for row in tracked_rows
                    if (row.mentioned or "").strip() in ["Yes", "Sometimes"]
                ]
                visibility = (
                    int((len(visible_rows) / total_rows) * 100)
                    if total_rows > 0
                    else 30
                )

                competitor_rows = [
                    row
                    for row in tracked_rows
                    if row.top_competitor and row.top_competitor != "—"
                ]
                competitors = (
                    int((len(competitor_rows) / total_rows) * 100)
                    if total_rows > 0
                    else 50
                )
            else:
                visibility = 30
                competitors = 50

            brief_text = (
                result.get("brief", "")
                if isinstance(result, dict)
                else str(result)
            )
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
            refund_credits(
                current_user,
                1,
                notes="Refund for failed content brief generation",
            )
            return render_template(
                "content_brief_form.html",
                client=client,
                error=f"Brief generation failed: {str(e)}",
                form_data=request.form,
            )

    prefill_query = safe_str(request.args.get("target_query"))
    prefill_content_type = (
        safe_str(request.args.get("content_type")) or "service_page"
    )
    prefill_context = safe_str(request.args.get("brand_context"))

    form_data = {
        "target_query": prefill_query if prefill_query else default_query,
        "content_type": prefill_content_type,
        "brand_context": (
            prefill_context if prefill_context else client.get("notes", "")
        ),
    }

    return render_template(
        "content_brief_form.html",
        client=client,
        error=None,
        form_data=form_data,
    )


@app.route("/generate-brief/<item_id>")
@login_required
def generate_brief_from_queue(item_id):
    item = get_queue_item_by_id(item_id, user_id=current_user.id)

    if not item:
        flash("Queue item not found.", "error")
        return redirect(url_for("content_queue_page"))

    client_slug = str(item.get("client_id", "")).strip()

    row = None
    if client_slug:
        row = Client.query.filter_by(
            slug=client_slug, user_id=current_user.id
        ).first()

    if not row:
        flash(
            "This queue item is linked to an old or missing workspace. Please recreate it from the current workspace.",
            "warning",
        )
        return redirect(url_for("content_queue_page"))

    client = get_client_by_id(row.slug)
    latest_audit = client.get("latest_audit") if client else None

    target_query = safe_str(item.get("target_query"))
    content_type = safe_str(item.get("content_type") or "service_page")

    # Prefer saved AI context from the queue item.
    brand_context = safe_str(item.get("content"))

    # Fallback: generate fresh AI-style context if queue item is empty.
    if not brand_context:
        brand_context = build_ai_brief_context(
            client=client or {},
            target_query=target_query,
            latest_audit=latest_audit,
        )

        update_queue_item_content(
            item_id=item_id,
            content=brand_context,
            user_id=current_user.id,
        )

    return redirect(
        url_for(
            "generate_client_content_brief",
            client_id=row.slug,
            target_query=target_query,
            content_type=content_type,
            brand_context=brand_context,
        )
    )


@app.route("/generate-draft/<item_id>")
@login_required
def generate_draft_from_queue(item_id):
    item = get_queue_item_by_id(item_id, user_id=current_user.id)

    if not item:
        flash("Queue item not found.", "error")
        return redirect(url_for("content_queue_page"))

    client_slug = str(item.get("client_id", "")).strip()

    row = None
    if client_slug:
        row = Client.query.filter_by(
            slug=client_slug, user_id=current_user.id
        ).first()

    if not row:
        flash(
            "This queue item is linked to an old or missing workspace. Please recreate it from the current workspace.",
            "warning",
        )
        return redirect(url_for("content_queue_page"))

    target_query = safe_str(item.get("target_query"))
    content_type = safe_str(item.get("content_type") or "service_page")
    brief_context = safe_str(item.get("content") or item.get("brief") or "")
    brand_context = safe_str(item.get("brand_context") or "")

    return redirect(
        url_for(
            "generate_client_content_draft",
            client_id=row.slug,
            target_query=target_query,
            content_type=content_type,
            brief_context=brief_context,
            brand_context=brand_context,
        )
    )


@app.route("/client/<client_id>/query-ideas/add", methods=["POST"])
@login_required
def add_query_idea_to_queue(client_id):
    client = get_client_by_id(client_id)

    if not client:
        abort(404)

    target_query = request.form.get("target_query", "").strip()
    content_type = request.form.get("content_type", "service_page").strip()

    if not target_query:
        flash("Please choose a query before adding it to the queue.", "error")
        return redirect(url_for("client_query_ideas", client_id=client_id))

    client_name = client.get("name", "Workspace")
    client_real_id = client.get("id", client_id)

    add_queue_item(
        client_id=client_real_id,
        client_name=client_name,
        target_query=target_query,
        content_type=content_type,
        item_type="brief",
        title=f"Suggested query: {target_query}",
        content="",
        status="pending",
        priority="medium",
        source="query_ideas",
        user_id=current_user.id,
    )

    flash("Query added to the content queue.", "success")
    return redirect(url_for("content_queue_page", client_id=client_real_id))


@app.route("/content-queue/<item_id>/delete", methods=["POST"])
@login_required
def delete_content_queue_item(item_id):
    item = get_queue_item_by_id(item_id, user_id=current_user.id)

    if not item:
        flash("Queue item not found.", "error")
        return redirect(url_for("content_queue_page"))

    client_id = item.get("client_id")

    deleted = delete_queue_item(
        item_id=item_id,
        user_id=current_user.id,
    )

    if deleted:
        flash("Queue item deleted.", "success")
    else:
        flash("Could not delete queue item.", "error")

    return redirect(url_for("content_queue_page", client_id=client_id))


@app.route("/content-queue/<item_id>/edit", methods=["POST"])
@login_required
def edit_content_queue_item(item_id):
    target_query = request.form.get("target_query", "").strip()
    title = request.form.get("title", "").strip()
    content_type = request.form.get("content_type", "service_page").strip()
    priority = request.form.get("priority", "medium").strip()

    if not target_query:
        flash("Target query cannot be empty.", "error")
        return redirect(url_for("content_queue_page"))

    updated_item = update_queue_item_details(
        item_id=item_id,
        title=title or f"Suggested query: {target_query}",
        target_query=target_query,
        content_type=content_type,
        priority=priority,
        user_id=current_user.id,
    )

    if not updated_item:
        flash("Queue item not found.", "error")
        return redirect(url_for("content_queue_page"))

    client_id = updated_item.get("client_id")
    flash("Queue item updated.", "success")
    return redirect(url_for("content_queue_page", client_id=client_id))


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
                "target_query": safe_str(request.form.get("target_query"))
                or default_query,
                "content_type": safe_str(
                    request.form.get("content_type", "service_page")
                )
                or "service_page",
                "brief_context": safe_str(request.form.get("brief_context")),
                "brand_context": safe_str(request.form.get("brand_context"))
                or client.get("notes", ""),
            }
            return render_template(
                "content_draft_form.html",
                client=client,
                error=None,
                form_data=form_data,
            )

        target_query = safe_str(request.form.get("target_query"))
        content_type = (
            safe_str(request.form.get("content_type", "service_page"))
            or "service_page"
        )
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
            flash(
                "You don’t have enough credits to generate another draft.",
                "warning",
            )
            return redirect(url_for("pricing_page"))

        if not spend_credits(
            current_user, 2, notes="Content draft generation"
        ):
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
            return render_template(
                "content_draft_result.html", client=client, result=result
            )

        except Exception as e:
            refund_credits(
                current_user,
                2,
                notes="Refund for failed content draft generation",
            )
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
        "brand_context": (
            prefill_brand_context
            if prefill_brand_context
            else client.get("notes", "")
        ),
    }
    return render_template(
        "content_draft_form.html",
        client=client,
        error=None,
        form_data=form_data,
    )


@app.route("/audit/<summary_filename>")
@login_required
def audit_summary(summary_filename):
    summary_path = get_summary_path(summary_filename)
    if not summary_path:
        abort(404)

    summary_filename = get_matching_summary_filename(summary_filename)
    summary_data = load_json_file(summary_path)
    full_filename = get_matching_full_filename(summary_filename)
    return render_template(
        "audit_summary.html",
        summary_filename=summary_filename,
        full_filename=full_filename,
        data=summary_data,
    )


@app.route("/audit/<summary_filename>/pdf")
@login_required
def audit_summary_pdf(summary_filename):
    summary_path = get_summary_path(summary_filename)
    if not summary_path:
        abort(404)

    summary_filename = get_matching_summary_filename(summary_filename)
    summary_data = load_json_file(summary_path)
    full_filename = get_matching_full_filename(summary_filename)
    report_date = datetime.utcnow().strftime("%d %b %Y")
    html = render_template(
        "audit_summary_pdf.html",
        summary_filename=summary_filename,
        full_filename=full_filename,
        data=summary_data,
        report_date=report_date,
    )

    website = summary_data.get("website") or summary_data.get("client_name")
    filename = pdf_filename(f"{website or 'audit'} summary")
    return render_pdf_response(
        html,
        filename,
        fallback_lines=audit_summary_pdf_lines(
            summary_data, summary_filename, report_date
        ),
    )


@app.route("/audit/<summary_filename>/full")
@login_required
def audit_full(summary_filename):
    require_internal_access()  # 👈 ADD THIS LINE

    full_path = get_full_path(summary_filename)
    if not full_path:
        abort(404)

    summary_filename = get_matching_summary_filename(summary_filename)
    full_data = load_json_file(full_path)
    full_filename = get_matching_full_filename(summary_filename)
    return render_template(
        "audit_full.html",
        summary_filename=summary_filename,
        full_filename=full_filename,
        data=full_data,
    )


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
    selected_market = request.args.get(
        "market", "United States (English)"
    ).strip()
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
        ai_answer = f"Current tracked visibility for this prompt is {
                row.visibility}. " f"Your brand mention status is {
                row.mentioned}. " f"Top competitor currently associated with this prompt is {
                row.top_competitor or 'unknown'}."
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
    client_id = request.form.get("client_id", "").strip()
    prompts = request.form.get("prompts", "").strip()
    domain = request.form.get("domain", "").strip()
    platform = request.form.get("platform", "ChatGPT").strip()
    market = request.form.get("market", "United States (English)").strip()
    topic = request.form.get("topic", "Tracked prompts").strip()

    selected_client = None
    if client_id:
        selected_client = get_client_by_id(client_id)

    if selected_client and not domain:
        domain = normalize_website(selected_client.get("website", ""))

    if not domain:
        domain = "supportfast.ai"

    prompt_list = [p.strip() for p in prompts.splitlines() if p.strip()]

    if not prompt_list:
        flash("No prompts entered.", "warning")
        if client_id:
            return redirect(
                url_for("position_tracking_page", client_id=client_id)
            )
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

    redirect_args = {
        "domain": domain,
        "platform": platform,
        "market": market,
        "topic": topic,
    }
    if client_id:
        redirect_args["client_id"] = client_id

    return redirect(url_for("position_tracking_page", **redirect_args))


@app.route("/client/<client_id>/competitors")
@login_required
def client_competitors_page(client_id):
    client = get_client_by_id(client_id)

    if not client:
        abort(404)

    # Free users can see audit + limited queue, but competitor analysis is
    # locked
    if current_user.plan == "free":
        flash(
            "Competitor analysis is available on Pro and Growth plans.", "info"
        )
        return redirect(url_for("pricing_page"))

    domain = request.args.get("domain", "").strip()
    competitor_1 = request.args.get("competitor_1", "").strip()
    competitor_2 = request.args.get("competitor_2", "").strip()
    competitor_3 = request.args.get("competitor_3", "").strip()
    competitor_4 = request.args.get("competitor_4", "").strip()

    project_domain = (
        domain
        or normalize_website(client.get("website", ""))
        or client.get("website", "")
        or ""
    )

    competitors = [
        c
        for c in [
            competitor_1,
            competitor_2,
            competitor_3,
            competitor_4,
        ]
        if c
    ]

    # sample fallback
    if not competitors and request.args.get("sample") == "1":
        competitors = [
            "tawk.to",
            "wati.io",
            "botpenguin.com",
            "chatmaxima.com",
        ]

    analysis_has_run = bool(
        domain or competitor_1 or competitor_2 or competitor_3 or competitor_4
    )

    return render_template(
        "client_competitors.html",
        client=client,
        project_domain=project_domain,
        competitors=competitors,
        selected_platform="ChatGPT",
        selected_market="United States (English)",
        analysis_has_run=analysis_has_run,
    )


@app.route("/client/<client_id>/actions")
@login_required
def client_actions_page(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)
    queue_items = get_queue_items(
        client_id=client.get("id"), user_id=current_user.id
    )
    next_best_action = build_resolved_next_action(
        client=client,
        queue_items=queue_items,
        has_clients=True,
        total_audits=client.get("audit_count", 0),
        total_prompts=len(client.get("tracked_prompts", []) or []),
    )
    return render_template(
        "client_actions.html",
        client=client,
        next_best_action=next_best_action,
    )


@app.route(
    "/client/<client_id>/competitor-topic/add-to-queue", methods=["POST"]
)
@login_required
def add_competitor_topic_to_queue(client_id):
    client = get_client_by_id(client_id)

    if not client:
        abort(404)

    topic = request.form.get("topic", "").strip()
    target_query = request.form.get("target_query", "").strip()
    content_type = request.form.get("content_type", "service_page").strip()
    best_competitor = request.form.get("best_competitor", "").strip()
    intent = request.form.get("intent", "").strip()
    priority = request.form.get("priority", "medium").strip().lower()
    sample_prompts = request.form.get("sample_prompts", "").strip()

    if not target_query:
        target_query = topic

    if not target_query:
        flash("No competitor opportunity was selected.", "error")
        return redirect(
            url_for("client_competitors_page", client_id=client_id)
        )

    if priority not in ["low", "medium", "high"]:
        priority = "medium"

    queue_context = f"""
Competitor gap opportunity

Topic:
{topic or "Not specified"}

Primary target query:
{target_query}

Best visible competitor:
{best_competitor or "Not specified"}

Intent:
{intent or "Not specified"}

Sample prompts:
{sample_prompts or target_query}

Recommended use:
Create a page or section that directly answers the selected prompt, explains the topic clearly, and gives stronger reasons for the brand to be included in AI answers.
""".strip()

    add_queue_item(
        client_id=client.get("id"),
        client_name=client.get("name", "Workspace"),
        target_query=target_query,
        content_type=content_type,
        item_type="brief",
        title=f"Competitor gap: {target_query}",
        content=queue_context,
        status="pending",
        priority=priority,
        source="competitor_research",
        user_id=current_user.id,
    )

    flash("Competitor opportunity added to the content queue.", "success")
    return redirect(url_for("content_queue_page", client_id=client.get("id")))


@app.route("/client/<client_id>/history")
@login_required
def client_history_page(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)
    return render_template("client_history.html", client=client)


@app.route("/position-tracking")
@login_required
def position_tracking_page():
    client_id = request.args.get("client_id", "").strip()
    domain = request.args.get("domain", "").strip()
    platform = request.args.get("platform", "ChatGPT").strip()
    market = request.args.get("market", "United States (English)").strip()
    topic = request.args.get("topic", "").strip()

    view_mode = get_view_mode(current_user)
    focused_client = get_focused_client_for_user(current_user)

    selected_client = None
    if client_id:
        selected_client = get_client_by_id(client_id)

    if not selected_client and view_mode == "single" and focused_client:
        selected_client = focused_client
        client_id = str(focused_client.get("id", "")).strip()

    if selected_client and not domain:
        domain = normalize_website(selected_client.get("website", ""))

    if not domain:
        domain = "supportfast.ai"

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
        tracked_prompts.append(
            {
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
            }
        )

    mentioned_count = sum(
        1 for row in tracked_prompts if row["mentioned"] == "Yes"
    )
    partial_count = sum(
        1 for row in tracked_prompts if row["mentioned"] == "Sometimes"
    )
    low_visibility_count = sum(
        1 for row in tracked_prompts if row["visibility"] == "Low"
    )
    highest_competitor = (
        tracked_prompts[0]["top_competitor"] if tracked_prompts else "—"
    )
    best_next_move = (
        "Build content for missing prompts"
        if low_visibility_count > 0
        else "Keep tracking visibility"
    )

    return render_template(
        "position_tracking.html",
        client_id=client_id,
        focused_client=focused_client,
        selected_client=selected_client,
        project_domain=domain,
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

        if item.get("id"):
            item["generate_brief_url"] = url_for(
                "generate_brief_from_queue", item_id=item["id"]
            )
            item["generate_draft_url"] = url_for(
                "generate_draft_from_queue", item_id=item["id"]
            )
        else:
            item["generate_brief_url"] = None
            item["generate_draft_url"] = None

    # Stats should use ALL filtered items, not only the current page.
    # "Ready" now means human-approved and awaiting publish — drafts mid-flow
    # (brief_generated, draft_generated) belong in "In Progress".
    stats = {
        "queued": len(
            [
                i
                for i in items
                if (i.get("status") or "").lower() in ["queued", "pending"]
            ]
        ),
        "in_progress": len(
            [
                i
                for i in items
                if (i.get("status") or "").lower()
                in [
                    "in_progress",
                    "in-progress",
                    "brief_generated",
                    "brief generated",
                    "draft_generated",
                    "draft generated",
                ]
            ]
        ),
        "ready": len(
            [
                i
                for i in items
                if (i.get("status") or "").lower() == "ready"
            ]
        ),
        "published": len(
            [
                i
                for i in items
                if (i.get("status") or "").lower() == "published"
            ]
        ),
    }

    selected_client = None
    if selected_client_id:
        selected_client = next(
            (
                client
                for client in clients
                if str(client.get("id")) == str(selected_client_id)
            ),
            None,
        )

    # Pagination
    page = request.args.get("page", 1, type=int)
    per_page = 10

    total_queue_items = len(items)
    total_pages = max((total_queue_items + per_page - 1) // per_page, 1)

    if page < 1:
        page = 1

    if page > total_pages:
        page = total_pages

    start = (page - 1) * per_page
    end = start + per_page

    visible_queue_items = items[start:end]

    return render_template(
        "content_queue.html",
        queue_items=visible_queue_items,
        total_queue_items=total_queue_items,
        current_page=page,
        total_pages=total_pages,
        per_page=per_page,
        selected_client_id=selected_client_id,
        selected_client=selected_client,
        clients=clients,
        focused_client=get_focused_client_for_user(current_user),
        view_mode=view_mode,
        stats=stats,
        incoming_query=incoming_query,
        incoming_topic=incoming_topic,
        incoming_source=incoming_source,
    )


@app.route("/content-queue/<item_id>/status", methods=["POST"])
@login_required
def update_content_queue_status(item_id):
    new_status = request.form.get("status", "pending").strip()
    item = update_queue_item_status(
        item_id, new_status, user_id=current_user.id
    )

    if not item:
        abort(404)

    client_id = request.form.get("client_id", "").strip()
    flash("Queue item status updated.")

    if client_id:
        return redirect(url_for("content_queue_page", client_id=client_id))
    return redirect(url_for("content_queue_page"))


def _redirect_to_queue(client_id):
    if client_id:
        return redirect(url_for("content_queue_page", client_id=client_id))
    return redirect(url_for("content_queue_page"))


@app.route("/content-queue/<item_id>/approve", methods=["POST"])
@login_required
def approve_content_queue_item(item_id):
    item, error = transition_queue_item(
        item_id, "approve", user_id=current_user.id
    )
    client_id = request.form.get("client_id", "").strip()

    if error:
        flash(error, "error")
    else:
        flash("Draft approved. Item is ready to publish.", "success")

    return _redirect_to_queue(client_id)


@app.route("/content-queue/<item_id>/publish", methods=["POST"])
@login_required
def publish_content_queue_item(item_id):
    item, error = transition_queue_item(
        item_id, "publish", user_id=current_user.id
    )
    client_id = request.form.get("client_id", "").strip()

    if error:
        flash(error, "error")
    else:
        flash("Item marked as published.", "success")

    return _redirect_to_queue(client_id)


CONTENT_TYPE_TO_WEBFLOW_COLLECTION = {
    "blog_post": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
    "article": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
    "faq_page": ("WEBFLOW_FAQ_COLLECTION_ID", "faq"),
    "faq": ("WEBFLOW_FAQ_COLLECTION_ID", "faq"),
    "service_page": ("WEBFLOW_SERVICE_COLLECTION_ID", "service"),
    "location_page": ("WEBFLOW_LOCATION_COLLECTION_ID", "location"),
    "comparison_page": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
    "landing_page": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
}

# Reverse: when an item is already published, look the collection back up by
# the stored label so updates always target the same CMS collection even if
# the user edits the item's content_type later.
COLLECTION_LABEL_TO_ENV = {
    "blog": "WEBFLOW_BLOG_COLLECTION_ID",
    "faq": "WEBFLOW_FAQ_COLLECTION_ID",
    "service": "WEBFLOW_SERVICE_COLLECTION_ID",
    "location": "WEBFLOW_LOCATION_COLLECTION_ID",
}

# Multi-page website export: map a page's page_type to the collection it
# should land in. "contact" is intentionally skipped — a contact page is
# usually a static designer page, not a CMS item.
PAGE_TYPE_TO_COLLECTION = {
    "home": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
    "about": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
    "blog": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
    "blog_post": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
    "landing": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
    "landing_page": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
    "comparison": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
    "comparison_page": ("WEBFLOW_BLOG_COLLECTION_ID", "blog"),
    "faq": ("WEBFLOW_FAQ_COLLECTION_ID", "faq"),
    "faq_page": ("WEBFLOW_FAQ_COLLECTION_ID", "faq"),
    "services": ("WEBFLOW_SERVICE_COLLECTION_ID", "service"),
    "service": ("WEBFLOW_SERVICE_COLLECTION_ID", "service"),
    "service_page": ("WEBFLOW_SERVICE_COLLECTION_ID", "service"),
    "location": ("WEBFLOW_LOCATION_COLLECTION_ID", "location"),
    "location_page": ("WEBFLOW_LOCATION_COLLECTION_ID", "location"),
}


def _build_field_data_for_generated_page(page, collection_label):
    """Build a CMS field-data payload from a GeneratedWebsitePage row."""
    title = page.title or page.slug or "Untitled"
    slug = _slugify_for_webflow(page.slug or title)
    page_json = page.page_json or {}

    # Pull body content from common locations the page builder produces.
    summary = ""
    body = ""
    if isinstance(page_json, dict):
        summary = (
            page_json.get("meta_description")
            or page_json.get("summary")
            or page_json.get("hero", {}).get("subhead", "")
        )
        body = page_json.get("body") or page_json.get("content") or ""

        # Fallback: stitch sections together.
        if not body and "sections" in page_json:
            body = "\n\n".join(
                str(s.get("body") or s.get("content") or "")
                for s in (page_json.get("sections") or [])
                if isinstance(s, dict)
            )

    fields = {"name": title, "slug": slug}
    if collection_label == "faq":
        fields["question"] = title
        fields["answer"] = body or summary or title
    else:
        if summary:
            fields["summary"] = summary[:240]
        if body:
            fields["content"] = body
    return fields


def _export_website_project_per_collection(project, pages):
    """Push each generated page to the collection that matches its page_type.

    Used when the legacy single-collection WEBFLOW_COLLECTION_ID isn't set
    but per-collection env vars are. Returns a result dict mirroring the
    legacy export's shape so callers can stay consistent.
    """
    from services.webflow_client import (
        WebflowCMSClient,
        WebflowAPIError,
        WebflowConfigError,
    )

    client = WebflowCMSClient()
    site_id = os.getenv("WEBFLOW_SITE_ID")

    exported_pages = []
    skipped_pages = []
    errors = []

    for page in pages:
        page_type = (page.page_type or "").lower()
        routing = PAGE_TYPE_TO_COLLECTION.get(page_type)
        if not routing:
            skipped_pages.append({"slug": page.slug, "reason": f"no collection mapped for '{page_type}'"})
            continue

        env_var, collection_label = routing
        collection_id = os.getenv(env_var)
        if not collection_id or collection_id.startswith("your_"):
            skipped_pages.append({"slug": page.slug, "reason": f"{collection_label} collection not configured"})
            continue

        field_data = _build_field_data_for_generated_page(page, collection_label)

        # Reuse an existing webflow_item_id if we tracked one previously.
        existing_wf = (page.page_json or {}).get("webflow") if isinstance(page.page_json, dict) else None
        existing_item_id = (existing_wf or {}).get("item_id")

        try:
            if existing_item_id:
                client.update_item(collection_id, existing_item_id, field_data)
                webflow_item_id = existing_item_id
                action = "updated"
            else:
                webflow_item_id = client.create_item(collection_id, field_data, is_draft=True)
                action = "created"
        except (WebflowAPIError, WebflowConfigError) as e:
            errors.append({"slug": page.slug, "error": str(e)})
            continue

        live_url = _build_live_url(client, collection_id, field_data["slug"])

        # Stamp the result back on the page so the preview can show it.
        if not isinstance(page.page_json, dict):
            page.page_json = {}
        page.page_json["webflow"] = {
            "item_id": webflow_item_id,
            "collection": collection_label,
            "collection_id": collection_id,
            "live_url": live_url,
            "last_action": action,
            "exported_at": datetime.utcnow().isoformat(timespec="seconds"),
        }
        flag_modified(page, "page_json")

        # Per-page export tracking row.
        try:
            export = WebflowExport(
                user_id=current_user.id,
                client_id=str(project.client_id) if project.client_id else None,
                content_type=collection_label,
                local_source_type="generated_page",
                local_source_id=str(page.id),
                webflow_site_id=site_id,
                webflow_collection_id=collection_id,
                webflow_item_id=webflow_item_id,
                status=action if action == "updated" else "exported",
                field_mapping=field_data,
            )
            db.session.add(export)
        except Exception as e:
            logger.warning(f"Per-page export tracking failed: {e}")

        exported_pages.append({
            "local_page_id": page.id,
            "slug": page.slug,
            "page_type": page_type,
            "collection": collection_label,
            "webflow_item_id": webflow_item_id,
            "action": action,
            "live_url": live_url,
        })

    db.session.commit()

    return {
        "site_id": site_id,
        "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "pages": exported_pages,
        "skipped": skipped_pages,
        "errors": errors,
        "published": False,  # Always drafts in this path
        "mode": "per_collection",
    }


def _slugify_for_webflow(text):
    """Webflow slugs: lowercase, alphanumerics + hyphens, no leading/trailing hyphens."""
    import re

    s = re.sub(r"[^\w\s-]", "", (text or "").lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:80] or "untitled"


# Module-level caches — site domain rarely changes; collection slugs even less.
_SITE_DOMAIN_CACHE = {}
_COLLECTION_SLUG_CACHE = {}
_COLLECTION_IMAGE_FIELD_CACHE = {}


def _get_collection_image_field_slug(client, collection_id):
    """Find the first image-type field on a collection, cached per collection.

    Webflow image field types come back as 'ImageRef' or 'Image' depending on
    the API version; check both.
    """
    if not collection_id:
        return None
    if collection_id in _COLLECTION_IMAGE_FIELD_CACHE:
        return _COLLECTION_IMAGE_FIELD_CACHE[collection_id]
    try:
        fields = client.list_collection_fields(collection_id)
    except Exception as e:
        logger.warning(f"Couldn't list fields for collection {collection_id}: {e}")
        return None

    image_slug = None
    for f in fields or []:
        ftype = (f.get("type") or "").lower()
        if ftype in {"image", "imageref"}:
            image_slug = f.get("slug")
            break

    _COLLECTION_IMAGE_FIELD_CACHE[collection_id] = image_slug
    return image_slug


def _get_site_default_domain(client, site_id):
    """Fetch and cache the site's primary domain.

    Resolution order, since the v2 API often returns empty customDomains and
    a None defaultDomain on hosted-only sites:
      1. customDomains[*].url
      2. defaultDomain
      3. <shortName>.webflow.io (the free hosted domain Webflow always serves)
    """
    if not site_id:
        return None
    if site_id in _SITE_DOMAIN_CACHE:
        return _SITE_DOMAIN_CACHE[site_id]
    try:
        info = client._request("GET", f"/sites/{site_id}")
    except Exception as e:
        logger.warning(f"Couldn't fetch site domain: {e}")
        return None

    domain = None
    for d in info.get("customDomains") or []:
        url = d.get("url") or d.get("name")
        if url:
            domain = url
            break
    if not domain:
        domain = info.get("defaultDomain")
    if not domain:
        short_name = info.get("shortName")
        if short_name:
            domain = f"{short_name}.webflow.io"

    if domain:
        domain = domain.replace("https://", "").replace("http://", "").rstrip("/")
        _SITE_DOMAIN_CACHE[site_id] = domain
    return domain


def _get_collection_slug(client, collection_id):
    """Fetch and cache a collection's URL slug."""
    if not collection_id:
        return None
    if collection_id in _COLLECTION_SLUG_CACHE:
        return _COLLECTION_SLUG_CACHE[collection_id]
    try:
        details = client.get_collection_details(collection_id)
    except Exception as e:
        logger.warning(f"Couldn't fetch collection {collection_id}: {e}")
        return None
    slug = details.get("slug")
    if slug:
        _COLLECTION_SLUG_CACHE[collection_id] = slug
    return slug


def _build_live_url(client, collection_id, item_slug):
    """Build the live URL for a published item, or None if anything is missing."""
    if not item_slug:
        return None
    site_id = os.getenv("WEBFLOW_SITE_ID")
    domain = _get_site_default_domain(client, site_id)
    collection_slug = _get_collection_slug(client, collection_id)
    if not domain or not collection_slug:
        return None
    return f"https://{domain}/{collection_slug}/{item_slug}"


def _build_webflow_field_data_for_queue_item(item, collection_kind):
    """Map a queue item's title/content/target_query into Webflow field-data."""
    title = item.get("title") or item.get("target_query") or "Untitled"
    slug = _slugify_for_webflow(title)
    content = item.get("content") or ""

    fields = {"name": title, "slug": slug}

    if collection_kind == "faq":
        fields["question"] = title
        fields["answer"] = content
    else:
        fields["content"] = content
        if item.get("target_query"):
            fields["summary"] = f"Target query: {item['target_query']}"

    return fields


@app.route("/content-queue/<item_id>/publish-to-webflow", methods=["POST"])
@login_required
def publish_queue_item_to_webflow(item_id):
    """Push a ready queue item to the right Webflow CMS collection."""
    item = get_queue_item_by_id(item_id, user_id=current_user.id)
    if not item:
        abort(404)

    client_id = request.form.get("client_id", "").strip() or item.get("client_id")

    status = (item.get("status") or "").lower()
    if status not in {"ready", "draft_generated"}:
        flash(
            "Approve the draft first — only ready items can publish to Webflow.",
            "error",
        )
        return _redirect_to_queue(client_id)

    content_type = (item.get("content_type") or "").lower()
    routing = CONTENT_TYPE_TO_WEBFLOW_COLLECTION.get(content_type)
    if not routing:
        flash(
            f"This site can't publish '{content_type}' content yet. "
            "Pick a blog, FAQ, service, or location item.",
            "error",
        )
        return _redirect_to_queue(client_id)

    env_var, collection_label = routing
    collection_id = os.getenv(env_var)
    if not collection_id or collection_id.startswith("your_"):
        flash(
            f"Publishing for {collection_label} pages isn't set up on this site yet. "
            "Reach out to your admin to enable it.",
            "error",
        )
        return _redirect_to_queue(client_id)

    try:
        from services.webflow_client import (
            WebflowCMSClient,
            WebflowAPIError,
            WebflowConfigError,
        )

        client = WebflowCMSClient()
        field_data = _build_webflow_field_data_for_queue_item(item, collection_label)
        webflow_item_id = client.create_item(
            collection_id, field_data, is_draft=True
        )

        live_url = _build_live_url(
            client, collection_id, field_data.get("slug")
        )

        update_queue_item_webflow_export(
            item_id,
            webflow_item_id=webflow_item_id,
            webflow_collection=collection_label,
            webflow_live_url=live_url,
            user_id=current_user.id,
        )

        try:
            export = WebflowExport(
                user_id=current_user.id,
                client_id=item.get("client_id"),
                content_type=collection_label,
                local_source_type="content_queue",
                local_source_id=str(item_id),
                webflow_site_id=os.getenv("WEBFLOW_SITE_ID"),
                webflow_collection_id=collection_id,
                webflow_item_id=webflow_item_id,
                status="exported",
                field_mapping=field_data,
            )
            db.session.add(export)
            db.session.commit()
        except Exception as track_err:
            logger.warning(f"Webflow export tracking failed: {track_err}")
            db.session.rollback()

        flash(
            f"Published as a {collection_label} draft on your site. "
            "Review and go live from your CMS.",
            "success",
        )

    except WebflowConfigError as e:
        logger.warning(f"Site publishing config issue: {e}")
        flash(
            "Publishing isn't fully set up on this site yet. "
            "Reach out to your admin to enable it.",
            "error",
        )
    except WebflowAPIError as e:
        logger.warning(f"Site publishing API error: {e}")
        flash(
            "We couldn't publish this item to your site. Try again, "
            "or reach out to your admin if the problem keeps happening.",
            "error",
        )
    except Exception as e:
        logger.error(f"Publish to site failed: {e}")
        flash("Publishing failed unexpectedly. Try again.", "error")

    return _redirect_to_queue(client_id)


@app.route("/content-queue/<item_id>/ai-edit", methods=["POST"])
@login_required
def ai_edit_queue_item(item_id):
    """Run a one-shot AI revision against a queue item's content.

    Returns JSON:
      { "ok": True,  "revised_content": "...", "summary": "Short note on what changed" }
      { "ok": False, "error":  "..." }
    """
    item = get_queue_item_by_id(item_id, user_id=current_user.id)
    if not item:
        return jsonify({"ok": False, "error": "Queue item not found."}), 404

    instruction = (request.form.get("instruction") or
                   (request.get_json(silent=True) or {}).get("instruction") or "").strip()
    if not instruction:
        return jsonify({"ok": False, "error": "Please describe the edit you want."}), 400

    current_content = item.get("content") or ""
    if not current_content:
        return jsonify({"ok": False, "error": "This item has no content to edit yet. Generate a brief or draft first."}), 400

    if not os.getenv("OPENAI_API_KEY"):
        return jsonify({"ok": False, "error": "AI editing isn't set up on this site yet. Reach out to your admin."}), 503

    try:
        from openai import OpenAI
        client = OpenAI()

        system_prompt = (
            "You are an editor revising marketing content based on a user's instruction.\n"
            "Return strict JSON with exactly these keys:\n"
            "  - revised_content: the full revised content, in the same format as the original (markdown if input is markdown, prose if input is prose, etc.)\n"
            "  - summary: a one-sentence note on what you changed.\n"
            "Preserve any structured sections (titles, headings, FAQ Q&A) the original had. "
            "Do not add lorem ipsum. If the instruction is unclear, make a sensible best-effort revision."
        )

        user_prompt = (
            f"INSTRUCTION:\n{instruction}\n\n"
            f"ORIGINAL CONTENT:\n{current_content}\n\n"
            "Return revised_content + summary as JSON."
        )

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
        )

        raw = response.choices[0].message.content or "{}"
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}

        revised = (parsed.get("revised_content") or "").strip()
        summary = (parsed.get("summary") or "").strip()

        if not revised:
            return jsonify({
                "ok": False,
                "error": "AI didn't return a usable revision. Try rephrasing your instruction.",
            }), 502

        return jsonify({
            "ok": True,
            "original_content": current_content,
            "revised_content": revised,
            "summary": summary,
            "instruction": instruction,
        })

    except Exception as e:
        logger.error(f"AI edit failed: {e}")
        return jsonify({"ok": False, "error": "AI revision failed unexpectedly. Try again."}), 500


@app.route("/content-queue/<item_id>/apply-ai-edit", methods=["POST"])
@login_required
def apply_ai_edit_queue_item(item_id):
    """Apply an AI-revised content blob to a queue item."""
    revised = (request.form.get("revised_content") or "").strip()
    if not revised:
        flash("No revision to apply.", "error")
        return _redirect_to_queue(request.form.get("client_id", ""))

    updated = update_queue_item_content(
        item_id, content=revised, user_id=current_user.id
    )
    if not updated:
        abort(404)

    client_id = request.form.get("client_id", "").strip() or updated.get("client_id")
    flash("AI revision applied to this item.", "success")
    return _redirect_to_queue(client_id)


@app.route("/content-queue/<item_id>/generate-visual", methods=["POST"])
@login_required
def generate_queue_item_visual(item_id):
    """Render an OG image / banner for a queue item via Placid."""
    item = get_queue_item_by_id(item_id, user_id=current_user.id)
    if not item:
        abort(404)

    client_id = request.form.get("client_id", "").strip() or item.get("client_id")

    template_uuid = os.getenv("PLACID_TEMPLATE_UUID_OG")
    if not os.getenv("PLACID_API_TOKEN") or not template_uuid:
        flash(
            "Visual generation isn't set up on this site yet. "
            "Reach out to your admin to enable it.",
            "error",
        )
        return _redirect_to_queue(client_id)

    try:
        from services.placid_client import (
            PlacidClient,
            PlacidAPIError,
            PlacidConfigError,
        )

        client = PlacidClient()
        # Map queue item content into the template's named layers.
        # Templates configured in Placid should expose these layer names.
        layers = {
            "headline": {"text": item.get("title") or "Untitled"},
        }
        if item.get("target_query"):
            layers["subhead"] = {"text": item.get("target_query")}
        if item.get("client_name"):
            layers["brand"] = {"text": item.get("client_name")}

        result = client.generate_image(
            template_uuid=template_uuid, layers=layers, wait=True
        )
        status = result.get("status")
        image_url = result.get("image_url")

        if status == "finished" and image_url:
            update_queue_item_og_image(
                item_id, og_image_url=image_url, user_id=current_user.id
            )

            # If this item already lives in the CMS, also push the image to
            # the matching image field so the live page picks it up.
            cms_synced = False
            cms_skipped_reason = None
            webflow_item_id = item.get("webflow_item_id")
            collection_label = (item.get("webflow_collection") or "").lower()
            if webflow_item_id and collection_label:
                env_var = COLLECTION_LABEL_TO_ENV.get(collection_label)
                collection_id = os.getenv(env_var) if env_var else None
                if collection_id and not collection_id.startswith("your_"):
                    try:
                        from services.webflow_client import (
                            WebflowCMSClient as _WebflowCMSClient,
                            WebflowAPIError as _WebflowAPIError,
                        )

                        wf_client = _WebflowCMSClient()
                        image_slug = _get_collection_image_field_slug(
                            wf_client, collection_id
                        )
                        if image_slug:
                            wf_client.update_item(
                                collection_id,
                                webflow_item_id,
                                {image_slug: image_url},
                            )
                            cms_synced = True
                        else:
                            cms_skipped_reason = "no image field on collection"
                    except _WebflowAPIError as e:
                        logger.warning(f"Visual CMS sync API error: {e}")
                        cms_skipped_reason = "CMS rejected the image update"
                    except Exception as e:
                        logger.warning(f"Visual CMS sync failed: {e}")
                        cms_skipped_reason = "CMS sync failed"

            if cms_synced:
                flash(
                    "Visual generated and synced to your CMS. "
                    "Re-publish from the CMS to push it live.",
                    "success",
                )
            elif cms_skipped_reason:
                flash(
                    f"Visual generated. CMS sync skipped — {cms_skipped_reason}.",
                    "warning",
                )
            else:
                flash("Visual generated and attached to this item.", "success")
        elif status == "queued":
            flash(
                "Visual is still rendering. Refresh in a few seconds — it'll show up automatically.",
                "info",
            )
        else:
            flash(
                "We couldn't generate a visual for this item. Try again, "
                "or check the title isn't empty.",
                "error",
            )

    except PlacidConfigError as e:
        logger.warning(f"Visual generation config issue: {e}")
        flash(
            "Visual generation isn't fully set up on this site yet. "
            "Reach out to your admin.",
            "error",
        )
    except PlacidAPIError as e:
        logger.warning(f"Visual generation API error: {e}")
        flash(
            "We couldn't reach the visual generator. Try again, or "
            "reach out to your admin.",
            "error",
        )
    except Exception as e:
        logger.error(f"Generate visual failed: {e}")
        flash("Visual generation failed unexpectedly. Try again.", "error")

    return _redirect_to_queue(client_id)


@app.route("/content-queue/<item_id>/update-on-site", methods=["POST"])
@login_required
def update_queue_item_on_site(item_id):
    """Push edits for a previously-published queue item back to its CMS entry."""
    item = get_queue_item_by_id(item_id, user_id=current_user.id)
    if not item:
        abort(404)

    client_id = request.form.get("client_id", "").strip() or item.get("client_id")

    webflow_item_id = item.get("webflow_item_id")
    collection_label = (item.get("webflow_collection") or "").lower()
    if not webflow_item_id or not collection_label:
        flash(
            "This item isn't on your site yet — publish it first.",
            "error",
        )
        return _redirect_to_queue(client_id)

    env_var = COLLECTION_LABEL_TO_ENV.get(collection_label)
    if not env_var:
        flash(
            f"Can't find the publishing collection for this item's '{collection_label}' type.",
            "error",
        )
        return _redirect_to_queue(client_id)

    collection_id = os.getenv(env_var)
    if not collection_id or collection_id.startswith("your_"):
        flash(
            f"Publishing for {collection_label} pages isn't set up on this site anymore. "
            "Reach out to your admin.",
            "error",
        )
        return _redirect_to_queue(client_id)

    try:
        from services.webflow_client import (
            WebflowCMSClient,
            WebflowAPIError,
            WebflowConfigError,
        )

        client = WebflowCMSClient()
        field_data = _build_webflow_field_data_for_queue_item(item, collection_label)
        client.update_item(collection_id, webflow_item_id, field_data)

        live_url = _build_live_url(
            client, collection_id, field_data.get("slug")
        )
        if live_url and live_url != item.get("webflow_live_url"):
            update_queue_item_webflow_export(
                item_id,
                webflow_item_id=webflow_item_id,
                webflow_collection=collection_label,
                webflow_live_url=live_url,
                user_id=current_user.id,
            )

        # Track the update in WebflowExport too.
        try:
            export = WebflowExport(
                user_id=current_user.id,
                client_id=item.get("client_id"),
                content_type=collection_label,
                local_source_type="content_queue",
                local_source_id=str(item_id),
                webflow_site_id=os.getenv("WEBFLOW_SITE_ID"),
                webflow_collection_id=collection_id,
                webflow_item_id=webflow_item_id,
                status="updated",
                field_mapping=field_data,
            )
            db.session.add(export)
            db.session.commit()
        except Exception as track_err:
            logger.warning(f"Update tracking failed: {track_err}")
            db.session.rollback()

        flash(
            f"Updated the {collection_label} entry on your site. "
            "Re-publish from your CMS to push the changes live.",
            "success",
        )

    except WebflowConfigError as e:
        logger.warning(f"Site update config issue: {e}")
        flash(
            "Publishing isn't fully set up on this site anymore. Reach out to your admin.",
            "error",
        )
    except WebflowAPIError as e:
        logger.warning(f"Site update API error: {e}")
        flash(
            "We couldn't update this item on your site. Try again, or reach out to your admin.",
            "error",
        )
    except Exception as e:
        logger.error(f"Update on site failed: {e}")
        flash("Updating the item failed unexpectedly. Try again.", "error")

    return _redirect_to_queue(client_id)


@app.route("/content-queue/<item_id>/unapprove", methods=["POST"])
@login_required
def unapprove_content_queue_item(item_id):
    item, error = transition_queue_item(
        item_id, "unapprove", user_id=current_user.id
    )
    client_id = request.form.get("client_id", "").strip()

    if error:
        flash(error, "error")
    else:
        flash("Approval revoked. Item moved back to draft.", "success")

    return _redirect_to_queue(client_id)


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

    audits = filter_audits(
        all_audits, search_term=search_term, audit_type=audit_type
    )
    audits = sort_audits(audits, sort_by=sort_by, order=order)

    return jsonify(
        {"count": len(audits), "total_count": len(all_audits), "items": audits}
    )


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
        flash(
            "Presentation mode is available for agency workspaces.", "warning"
        )
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
        slug=client_id, user_id=current_user.id
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
    queue_items = get_queue_items(
        client_id=full_client.get("id"), user_id=current_user.id
    )
    next_best_action = build_resolved_next_action(
        client=full_client,
        queue_items=queue_items,
        has_clients=True,
        total_audits=full_client.get("audit_count", 0),
        total_prompts=len(full_client.get("tracked_prompts", []) or []),
    )

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
        next_best_action=next_best_action,
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
                preselected_client_id=(
                    str(focused_client["id"])
                    if (view_mode == "single" and focused_client)
                    else (clients[0]["id"] if len(clients) == 1 else None)
                ),
                form_data=request.form,
                error="Please choose a workspace.",
                view_mode=view_mode,
            )

        if not website or not industry or not location:
            return render_template(
                "new_audit.html",
                clients=clients,
                preselected_client_id=(
                    str(focused_client["id"])
                    if (view_mode == "single" and focused_client)
                    else (clients[0]["id"] if len(clients) == 1 else None)
                ),
                form_data=request.form,
                error="Website, industry, and location are required.",
                view_mode=view_mode,
            )

        if not has_enough_credits(current_user, 1):
            flash(
                "You don’t have enough credits to run another audit.",
                "warning",
            )
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

            created_count = create_content_opportunities_from_latest_audit(
                client_id=client_id,
                user_id=current_user.id,
            )

            if created_count > 0:
                flash(
                    f"Audit completed successfully. {created_count} content opportunities added to the queue.",
                    "success",
                )
            else:
                flash(
                    "Audit completed successfully. No new content opportunities were added.",
                    "success",
                )

            return redirect(url_for("client_detail", client_id=client_id))

        except Exception as e:
            refund_credits(
                current_user, 1, notes="Refund for failed new audit"
            )
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
        prefilled_client = next(
            (c for c in clients if str(c["id"]) == requested_client_id), None
        )
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
        "website": (
            prefilled_client.get("website", "") if prefilled_client else ""
        ),
        "industry": (
            prefilled_client.get("industry", "") if prefilled_client else ""
        ),
        "location": (
            prefilled_client.get("location", "") if prefilled_client else ""
        ),
        "topic": (
            prefilled_client.get("industry", "") if prefilled_client else ""
        ),
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
    require_internal_access()

    full_path = get_full_path(summary_filename)
    if not full_path:
        return jsonify({"error": "Full file not found"}), 404

    full_data = load_json_file(full_path)
    full_filename = get_matching_full_filename(summary_filename)
    return jsonify(
        {
            "summary_filename": summary_filename,
            "full_filename": full_filename,
            "data": full_data,
        }
    )


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
        can_add_workspace = (
            workspace_limit is None or workspace_count < workspace_limit
        )
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
            current_user.is_authenticated
            and (has_unlimited_credits or credit_balance_numeric >= 1)
        ),
        "can_generate_brief": (
            current_user.is_authenticated
            and (has_unlimited_credits or credit_balance_numeric >= 1)
        ),
        "can_generate_draft": (
            current_user.is_authenticated
            and (has_unlimited_credits or credit_balance_numeric >= 2)
        ),
    }


@app.route("/aeo-agency")
def aeo_agency_page():
    return render_template("landing_aeo.html")


def render_settings_section(section, **extra_context):
    is_internal_user = current_user.is_authenticated and (
        current_user.email == "pypteltd@gmail.com"
        or getattr(current_user, "role", "") == "admin"
        or getattr(current_user, "plan", "") == "dev_unlimited"
    )

    referral_link = None
    if current_user.is_authenticated and current_user.referral_code:
        referral_link = (
            request.host_url.rstrip("/")
            + url_for("signup")
            + "?ref="
            + current_user.referral_code
        )

    credit_history = []
    if current_user.is_authenticated:
        try:
            credit_history = (
                CreditTransaction.query
                .filter_by(user_id=current_user.id)
                .order_by(CreditTransaction.created_at.desc())
                .limit(20)
                .all()
            )
        except Exception:
            credit_history = []

    referrals_made = []
    if current_user.is_authenticated:
        try:
            referrals_made = (
                Referral.query
                .filter_by(referrer_user_id=current_user.id)
                .order_by(Referral.created_at.desc())
                .limit(20)
                .all()
            )
        except Exception:
            referrals_made = []

    context = {
        "active_settings_section": section,
        "is_internal_user": is_internal_user,
        "view_mode": session.get("dev_view_mode", "auto"),
        "referral_link": referral_link,
        "credit_history": credit_history,
        "referrals_made": referrals_made,
    }
    context.update(extra_context)

    return render_template("settings.html", **context)


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


# =========================
# Webflow Integration Routes
# =========================
# These routes allow DarInsights to act as an AI CMS editor for Webflow sites.
# Users can export content (blog posts, FAQs, services) from DarInsights to Webflow CMS
# without directly editing Webflow Designer. All exports are created as drafts by default.


@app.route("/integrations/webflow/settings")
@login_required
def webflow_settings():
    """
    Display Webflow integration settings and connection status.
    
    Shows:
    - Whether Webflow is configured
    - Available collections
    - Connection status and test result
    - Instructions for setup
    """
    try:
        from services.webflow_client import WebflowCMSClient, WebflowConfigError
        
        config_error = None
        client = None
        collections = []
        connection_status = None
        
        try:
            client = WebflowCMSClient()
            client.test_connection()
            connection_status = "connected"
            collections = client.list_collections()
        except WebflowConfigError as e:
            config_error = str(e)
        except Exception as e:
            connection_status = f"error: {str(e)}"
            config_error = "Unable to connect to Webflow"
        
        # Get collection IDs from env
        blog_collection_id = os.getenv("WEBFLOW_BLOG_COLLECTION_ID")
        faq_collection_id = os.getenv("WEBFLOW_FAQ_COLLECTION_ID")
        service_collection_id = os.getenv("WEBFLOW_SERVICE_COLLECTION_ID")
        location_collection_id = os.getenv("WEBFLOW_LOCATION_COLLECTION_ID")
        
        return render_template(
            "integrations/webflow_settings.html",
            config_error=config_error,
            connection_status=connection_status,
            collections=collections,
            blog_collection_id=blog_collection_id,
            faq_collection_id=faq_collection_id,
            service_collection_id=service_collection_id,
            location_collection_id=location_collection_id,
            publish_on_export=os.getenv("WEBFLOW_PUBLISH_ON_EXPORT", "false").lower() in {"true", "1", "yes"},
        )
    except Exception as e:
        flash(f"Error loading Webflow settings: {str(e)}", "error")
        return redirect(url_for("index"))


@app.route("/integrations/webflow/test", methods=["POST"])
@login_required
def webflow_test_connection():
    """Test Webflow API connection and return status."""
    try:
        from services.webflow_client import WebflowCMSClient, WebflowAPIError, WebflowConfigError
        
        client = WebflowCMSClient()
        client.test_connection()
        
        return jsonify({
            "success": True,
            "message": "Successfully connected to Webflow API"
        })
    except (WebflowConfigError, WebflowAPIError) as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 400
    except Exception as e:
        logger.error(f"Webflow connection test failed: {e}")
        return jsonify({
            "success": False,
            "message": "Connection test failed"
        }), 500


@app.route("/integrations/webflow/collections")
@login_required
def webflow_collections():
    """List all Webflow collections for the site."""
    try:
        from services.webflow_client import WebflowCMSClient, WebflowAPIError, WebflowConfigError
        
        client = WebflowCMSClient()
        collections = client.list_collections()
        
        return jsonify({
            "success": True,
            "collections": collections
        })
    except (WebflowConfigError, WebflowAPIError) as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 400
    except Exception as e:
        logger.error(f"Failed to list collections: {e}")
        return jsonify({
            "success": False,
            "message": "Failed to list collections"
        }), 500


@app.route("/integrations/webflow/export/blog/<int:item_id>", methods=["POST"])
@login_required
def webflow_export_blog(item_id):
    """
    Export a blog post/content brief to Webflow blog collection.
    
    Expected JSON body:
    {
        "title": "Blog Post Title",
        "slug": "blog-post-slug",
        "summary": "Brief summary",
        "content": "HTML or rich text content",
        "meta_description": "SEO meta description",
        "featured_image_url": "optional image URL"
    }
    """
    try:
        from services.webflow_client import WebflowCMSClient, WebflowAPIError, WebflowConfigError
        
        blog_collection_id = os.getenv("WEBFLOW_BLOG_COLLECTION_ID")
        if not blog_collection_id or blog_collection_id.startswith("your_"):
            return jsonify({
                "success": False,
                "message": "Webflow blog collection not configured. Set WEBFLOW_BLOG_COLLECTION_ID in .env"
            }), 400
        
        data = request.get_json() or {}
        client = WebflowCMSClient()
        
        # Build field data for Webflow
        field_data = {
            "name": data.get("title", f"Blog Post {item_id}"),
            "slug": data.get("slug", f"blog-post-{item_id}"),
        }
        
        # Add optional fields if provided
        if data.get("summary"):
            field_data["summary"] = data["summary"]
        if data.get("content"):
            field_data["content"] = data["content"]
        if data.get("meta_description"):
            field_data["meta-description"] = data["meta_description"]
        
        # Create or update item in Webflow
        try:
            # Check if item already exists in our export tracking
            existing_export = WebflowExport.query.filter_by(
                user_id=current_user.id,
                local_source_id=item_id,
                content_type="blog"
            ).first()
            
            if existing_export and existing_export.webflow_item_id:
                # Update existing item
                result = client.update_item(blog_collection_id, existing_export.webflow_item_id, field_data)
                webflow_item_id = existing_export.webflow_item_id
                action = "updated"
            else:
                # Create new item
                webflow_item_id = client.create_item(blog_collection_id, field_data, is_draft=True)
                action = "created"
            
            # Track the export in our database
            if existing_export:
                existing_export.status = "exported"
                existing_export.field_mapping = field_data
                existing_export.error_message = None
            else:
                existing_export = WebflowExport(
                    user_id=current_user.id,
                    client_id=data.get("client_id"),
                    content_type="blog",
                    local_source_type="content_brief",
                    local_source_id=item_id,
                    webflow_site_id=os.getenv("WEBFLOW_SITE_ID"),
                    webflow_collection_id=blog_collection_id,
                    webflow_item_id=webflow_item_id,
                    status="exported",
                    field_mapping=field_data,
                )
                db.session.add(existing_export)
            
            db.session.commit()
            
            return jsonify({
                "success": True,
                "message": f"Blog post {action} in Webflow",
                "webflow_item_id": webflow_item_id,
                "action": action,
                "export_id": existing_export.id
            })
            
        except WebflowAPIError as e:
            # Track the failed export
            export = WebflowExport(
                user_id=current_user.id,
                client_id=data.get("client_id"),
                content_type="blog",
                local_source_type="content_brief",
                local_source_id=item_id,
                webflow_site_id=os.getenv("WEBFLOW_SITE_ID"),
                webflow_collection_id=blog_collection_id,
                status="failed",
                error_message=str(e),
                field_mapping=field_data,
            )
            db.session.add(export)
            db.session.commit()
            
            return jsonify({
                "success": False,
                "message": f"Failed to export to Webflow: {str(e)}",
                "export_id": export.id
            }), 400
    
    except Exception as e:
        logger.error(f"Blog export failed: {e}")
        return jsonify({
            "success": False,
            "message": "Export failed"
        }), 500


@app.route("/integrations/webflow/export/faq/<int:item_id>", methods=["POST"])
@login_required
def webflow_export_faq(item_id):
    """
    Export an FAQ item to Webflow FAQ collection.
    
    Expected JSON body:
    {
        "question": "Frequently asked question?",
        "answer": "Answer to the question",
        "category": "optional category"
    }
    """
    try:
        from services.webflow_client import WebflowCMSClient, WebflowAPIError, WebflowConfigError
        
        faq_collection_id = os.getenv("WEBFLOW_FAQ_COLLECTION_ID")
        if not faq_collection_id or faq_collection_id.startswith("your_"):
            return jsonify({
                "success": False,
                "message": "Webflow FAQ collection not configured. Set WEBFLOW_FAQ_COLLECTION_ID in .env"
            }), 400
        
        data = request.get_json() or {}
        client = WebflowCMSClient()
        
        field_data = {
            "name": data.get("question", f"FAQ Item {item_id}"),
        }
        
        if data.get("answer"):
            field_data["answer"] = data["answer"]
        if data.get("category"):
            field_data["category"] = data["category"]
        
        try:
            existing_export = WebflowExport.query.filter_by(
                user_id=current_user.id,
                local_source_id=item_id,
                content_type="faq"
            ).first()
            
            if existing_export and existing_export.webflow_item_id:
                result = client.update_item(faq_collection_id, existing_export.webflow_item_id, field_data)
                webflow_item_id = existing_export.webflow_item_id
                action = "updated"
            else:
                webflow_item_id = client.create_item(faq_collection_id, field_data, is_draft=True)
                action = "created"
            
            if existing_export:
                existing_export.status = "exported"
                existing_export.field_mapping = field_data
                existing_export.error_message = None
            else:
                existing_export = WebflowExport(
                    user_id=current_user.id,
                    client_id=data.get("client_id"),
                    content_type="faq",
                    local_source_type="faq_item",
                    local_source_id=item_id,
                    webflow_site_id=os.getenv("WEBFLOW_SITE_ID"),
                    webflow_collection_id=faq_collection_id,
                    webflow_item_id=webflow_item_id,
                    status="exported",
                    field_mapping=field_data,
                )
                db.session.add(existing_export)
            
            db.session.commit()
            
            return jsonify({
                "success": True,
                "message": f"FAQ item {action} in Webflow",
                "webflow_item_id": webflow_item_id,
                "action": action,
                "export_id": existing_export.id
            })
            
        except WebflowAPIError as e:
            export = WebflowExport(
                user_id=current_user.id,
                client_id=data.get("client_id"),
                content_type="faq",
                local_source_type="faq_item",
                local_source_id=item_id,
                webflow_site_id=os.getenv("WEBFLOW_SITE_ID"),
                webflow_collection_id=faq_collection_id,
                status="failed",
                error_message=str(e),
                field_mapping=field_data,
            )
            db.session.add(export)
            db.session.commit()
            
            return jsonify({
                "success": False,
                "message": f"Failed to export to Webflow: {str(e)}",
                "export_id": export.id
            }), 400
    
    except Exception as e:
        logger.error(f"FAQ export failed: {e}")
        return jsonify({
            "success": False,
            "message": "Export failed"
        }), 500


@app.route("/integrations/webflow/export/service/<int:item_id>", methods=["POST"])
@login_required
def webflow_export_service(item_id):
    """
    Export a service item to Webflow service collection.
    
    Expected JSON body:
    {
        "title": "Service Name",
        "slug": "service-slug",
        "description": "Service description",
        "price": "optional price info"
    }
    """
    try:
        from services.webflow_client import WebflowCMSClient, WebflowAPIError, WebflowConfigError
        
        service_collection_id = os.getenv("WEBFLOW_SERVICE_COLLECTION_ID")
        if not service_collection_id or service_collection_id.startswith("your_"):
            return jsonify({
                "success": False,
                "message": "Webflow service collection not configured. Set WEBFLOW_SERVICE_COLLECTION_ID in .env"
            }), 400
        
        data = request.get_json() or {}
        client = WebflowCMSClient()
        
        field_data = {
            "name": data.get("title", f"Service {item_id}"),
            "slug": data.get("slug", f"service-{item_id}"),
        }
        
        if data.get("description"):
            field_data["description"] = data["description"]
        if data.get("price"):
            field_data["price"] = data["price"]
        
        try:
            existing_export = WebflowExport.query.filter_by(
                user_id=current_user.id,
                local_source_id=item_id,
                content_type="service"
            ).first()
            
            if existing_export and existing_export.webflow_item_id:
                result = client.update_item(service_collection_id, existing_export.webflow_item_id, field_data)
                webflow_item_id = existing_export.webflow_item_id
                action = "updated"
            else:
                webflow_item_id = client.create_item(service_collection_id, field_data, is_draft=True)
                action = "created"
            
            if existing_export:
                existing_export.status = "exported"
                existing_export.field_mapping = field_data
                existing_export.error_message = None
            else:
                existing_export = WebflowExport(
                    user_id=current_user.id,
                    client_id=data.get("client_id"),
                    content_type="service",
                    local_source_type="service_item",
                    local_source_id=item_id,
                    webflow_site_id=os.getenv("WEBFLOW_SITE_ID"),
                    webflow_collection_id=service_collection_id,
                    webflow_item_id=webflow_item_id,
                    status="exported",
                    field_mapping=field_data,
                )
                db.session.add(existing_export)
            
            db.session.commit()
            
            return jsonify({
                "success": True,
                "message": f"Service item {action} in Webflow",
                "webflow_item_id": webflow_item_id,
                "action": action,
                "export_id": existing_export.id
            })
            
        except WebflowAPIError as e:
            export = WebflowExport(
                user_id=current_user.id,
                client_id=data.get("client_id"),
                content_type="service",
                local_source_type="service_item",
                local_source_id=item_id,
                webflow_site_id=os.getenv("WEBFLOW_SITE_ID"),
                webflow_collection_id=service_collection_id,
                status="failed",
                error_message=str(e),
                field_mapping=field_data,
            )
            db.session.add(export)
            db.session.commit()
            
            return jsonify({
                "success": False,
                "message": f"Failed to export to Webflow: {str(e)}",
                "export_id": export.id
            }), 400
    
    except Exception as e:
        logger.error(f"Service export failed: {e}")
        return jsonify({
            "success": False,
            "message": "Export failed"
        }), 500


if __name__ == "__main__":
    ensure_data_dirs()
    with app.app_context():
        db.create_all()
    print("Starting Flask app...")
    app.run(host="127.0.0.1", port=5001, debug=True)
