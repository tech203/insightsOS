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
    upsert_generation_item,
    append_queue_item_chat_messages,
    clear_queue_item_chat_history,
    delete_queue_item,
    transition_queue_item,
)
from help_content import HELP_GLOSSARY
from content_draft_generator import generate_content_draft
from content_brief_generator import generate_content_brief
from audit_runner import run_audit_for_input
import json
import logging
import secrets
from typing import Any, Dict, List, Optional
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
from pricing import (
    ACTION_CREDIT_COSTS,
    EXTRA_WORKSPACE_ADDON_PRICE_USD,
    PLAN_CATALOG,
    active_queue_limit_for_plan,
    baseline_credit_price,
    get_action_cost,
    get_bundles_for_plan,
    get_plan,
    is_subscriber,
    list_public_plans,
    monthly_credit_allowance,
    plan_allows_workspace_addon,
    workspace_limit_for_plan,
)
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
from datetime import datetime, timedelta
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
        "PERPLEXITY_API_KEY": "second AI engine for the Answer Monitor",
        "GOOGLE_CLIENT_ID": "Google Search Console connector (Pro/Growth)",
        "GOOGLE_CLIENT_SECRET": "Google Search Console connector (Pro/Growth)",
        "WEBFLOW_API_TOKEN": "Webflow publishing",
        "WEBFLOW_SITE_ID": "Webflow publishing",
        "WEBFLOW_COLLECTION_ID": "legacy single-collection export",
        "WEBFLOW_BLOG_COLLECTION_ID": "blog publishing",
        "WEBFLOW_FAQ_COLLECTION_ID": "FAQ publishing",
        "WEBFLOW_SERVICE_COLLECTION_ID": "service-page publishing",
        "WEBFLOW_LOCATION_COLLECTION_ID": "location-page publishing",
        "PLACID_API_TOKEN": "visual generation (OG images, banners)",
        "PLACID_TEMPLATE_UUID_OG": "OG image template for queue items",
        "SHOPIFY_API_KEY": "Shopify store OAuth + product sync",
        "SHOPIFY_API_SECRET": "Shopify store OAuth + product sync",
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
app.config["AGENCY_LOGO_FOLDER"] = os.path.join(
    "static", "uploads", "agency_logos"
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
    # Agency white-label fields. When is_white_label_enabled=True these
    # replace the DarInsights brand on PDFs, the sidebar brand mark, and
    # any client-facing surfaces invited team members see.
    agency_tagline = db.Column(db.String(255), nullable=True)
    agency_website = db.Column(db.String(500), nullable=True)
    agency_footer = db.Column(db.String(500), nullable=True)
    agency_disclaimer = db.Column(db.String(500), nullable=True)
    agency_logo_filename = db.Column(db.String(255), nullable=True)

    # Set by the Stripe webhook on the first successful checkout so
    # billing-portal sessions can locate the customer.
    stripe_customer_id = db.Column(db.String(120), nullable=True)
    stripe_subscription_id = db.Column(db.String(120), nullable=True)

    # Extra workspaces purchased beyond the plan's base cap (paid plans
    # only). $9 / extra workspace / month — billed via a recurring
    # Stripe subscription item, count synced from the
    # customer.subscription.updated webhook.
    extra_workspaces = db.Column(db.Integer, default=0, nullable=False)

    # Extra team seats purchased beyond the plan's base cap (Pro/Growth
    # only). $5 / extra seat / month.
    extra_seats = db.Column(db.Integer, default=0, nullable=False)

    # If non-null, this user is a team member belonging to the owner
    # account at team_owner_id. They see the owner's workspaces and
    # spend the owner's credits; their own User row exists only for
    # auth + audit log purposes.
    team_owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True, index=True
    )

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


class PasswordResetToken(db.Model):
    """One-time password-reset token. Single-use: marked `used_at`
    when consumed so the same email link can't be replayed. Tokens
    expire 60 min after issue."""
    __tablename__ = "password_reset_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    token = db.Column(db.String(80), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class TeamInvite(db.Model):
    """A pending invite to join an owner's team. The owner sends the
    /team/accept/<token> URL to the invitee; on accept, a User row is
    created (or attached if one with that email already exists) and
    that user's team_owner_id is set to the inviter.

    `status` lifecycle: pending → accepted | revoked. Pending invites
    count toward the owner's seat usage so the owner can't oversell."""
    __tablename__ = "team_invites"

    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    email = db.Column(db.String(255), nullable=False)
    token = db.Column(db.String(80), unique=True, nullable=False, index=True)
    status = db.Column(db.String(40), default="pending", nullable=False)
    invited_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    accepted_at = db.Column(db.DateTime, nullable=True)
    accepted_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
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
    """Referral payout record.

    Reward model: the referrer earns 25% of the dollar value the
    referred user pays (subscription or credit bundle), capped to
    purchases made within 30 days of signup. Free signups earn
    nothing — the reward only ever fires when the referred user
    actually pays. Each successful payment within the window creates
    a new Referral row in `rewarded` status; status `expired` records
    a window that closed without any payment.
    """
    __tablename__ = "referrals"

    id = db.Column(db.Integer, primary_key=True)
    referrer_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    referred_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False
    )
    referral_code = db.Column(db.String(50), nullable=False)
    # pending → rewarded | expired
    status = db.Column(db.String(50), default="pending", nullable=False)
    # Dollar amount the referred user paid on this triggering purchase
    # (cents not used — Stripe gives us amount_total in cents, we
    # divide on persist).
    referred_payment_usd = db.Column(db.Numeric(10, 2), nullable=True)
    # 25% of referred_payment_usd, in credits (1 credit = $1 baseline).
    reward_credits_referrer = db.Column(db.Integer, default=0, nullable=False)
    # Stripe object that triggered the reward (Checkout Session id),
    # so the same payment can never grant the referrer twice.
    stripe_event_ref = db.Column(db.String(120), nullable=True)
    qualified_at = db.Column(db.DateTime, nullable=True)
    rewarded_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )


class UserModule(db.Model):
    """A single module subscription for a user.

    Modular billing model: customers buy one Stripe Subscription with
    one line item per active module. Each line item maps to a row here
    via stripe_subscription_item_id — that's the granular handle we use
    to add or remove individual modules without canceling the whole
    sub.

    Re-subscriptions create new rows so we keep history. Active access
    is the union of rows where status='active'; see user_has_module()
    in the helpers section below for the lookup.

    Foundation only — no checkout routes are wired yet. The webhook
    handler writes rows when a `kind=modules` checkout completes, but
    until the dashboard surfaces module purchase, this table stays
    empty in production.
    """
    __tablename__ = "user_modules"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    module_slug = db.Column(db.String(50), nullable=False, index=True)
    stripe_subscription_id = db.Column(db.String(120), nullable=True, index=True)
    stripe_subscription_item_id = db.Column(
        db.String(120), nullable=True, unique=True
    )
    # Mirror of Stripe values: active | canceled | past_due | incomplete
    status = db.Column(db.String(30), nullable=False, default="active", index=True)
    current_period_end = db.Column(db.DateTime, nullable=True)
    activated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    deactivated_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class InterestSignup(db.Model):
    """Landing-page interest list signups.

    Backs the simple CRM at /admin/interest. Admins can update the
    status, add notes, and tag entries as they work the list.

    Status lifecycle:
      new        — fresh signup, not yet contacted
      contacted  — admin sent the first reachout
      replied    — prospect responded
      converted  — became a paying customer / scheduled call
      declined   — uninterested or unqualified
      unsubscribed — opted out of further contact
    """
    __tablename__ = "interest_signups"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    company = db.Column(db.String(255), nullable=True)
    use_case = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(30), default="new", nullable=False, index=True)
    notes = db.Column(db.Text, nullable=True)
    tags = db.Column(db.JSON, nullable=True)
    source = db.Column(db.String(80), default="landing", nullable=True)
    utm_source = db.Column(db.String(120), nullable=True)
    utm_medium = db.Column(db.String(120), nullable=True)
    utm_campaign = db.Column(db.String(120), nullable=True)
    referrer = db.Column(db.String(500), nullable=True)
    user_agent = db.Column(db.String(500), nullable=True)
    last_contacted_at = db.Column(db.DateTime, nullable=True)
    converted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class EmailCampaign(db.Model):
    """An email marketing campaign — composed by an admin, sent to a
    filtered audience drawn from interest signups + users.

    Status flow: draft → scheduled → sending → sent (or failed). Stats
    are aggregated from EmailCampaignRecipient rows so a campaign's
    sent/delivered/opened/clicked counts are always live.
    """
    __tablename__ = "email_campaigns"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    body_html = db.Column(db.Text, nullable=True)
    body_text = db.Column(db.Text, nullable=True)
    # audience_filter examples:
    #   {"source": "interest", "status": ["new", "contacted"]}
    #   {"source": "users", "plan": ["pro", "growth"]}
    #   {"source": "manual", "emails": ["a@b.com", "c@d.com"]}
    audience_filter = db.Column(db.JSON, nullable=True)
    sender_email = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(30), default="draft", nullable=False, index=True)
    created_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    sent_at = db.Column(db.DateTime, nullable=True)
    sent_count = db.Column(db.Integer, default=0, nullable=False)
    failed_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class EmailCampaignRecipient(db.Model):
    """One row per (campaign × recipient). Status tracks delivery; the
    optional message_id lets us match Resend webhook events back to
    the row when we wire delivered/opened/clicked tracking later."""
    __tablename__ = "email_campaign_recipients"

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(
        db.Integer, db.ForeignKey("email_campaigns.id"), nullable=False, index=True
    )
    email = db.Column(db.String(255), nullable=False, index=True)
    # source signals where the recipient came from so we can dedupe.
    source = db.Column(db.String(40), nullable=True)  # interest | user | manual
    source_id = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(30), default="pending", nullable=False, index=True)
    message_id = db.Column(db.String(255), nullable=True)
    error = db.Column(db.String(500), nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


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

    # Brand Kit Studio fields. Structured (one column per attribute)
    # so generation steps can lift values directly instead of parsing
    # the legacy `notes` blob. The blueprint calls this "foundational
    # architecture" — keep these stable.
    brand_audience = db.Column(db.Text, nullable=True)
    brand_services = db.Column(db.Text, nullable=True)
    brand_differentiators = db.Column(db.Text, nullable=True)
    brand_voice = db.Column(db.String(255), nullable=True)
    brand_personality = db.Column(db.String(255), nullable=True)
    brand_avoid = db.Column(db.Text, nullable=True)
    brand_primary_color = db.Column(db.String(20), nullable=True)
    brand_secondary_color = db.Column(db.String(20), nullable=True)
    brand_accent_color = db.Column(db.String(20), nullable=True)
    brand_typography = db.Column(db.String(120), nullable=True)
    brand_imagery_direction = db.Column(db.Text, nullable=True)
    brand_kit_updated_at = db.Column(db.DateTime, nullable=True)
    # When set, the Brand Kit has been approved by the user — the
    # blueprint's "Brand Kit → Preview → Approval" loop. Generators
    # check this before lifting kit values into prompts so unfinished
    # drafts don't bake half-formed direction into output. Cleared
    # automatically whenever a kit field changes via the studio so
    # the user has to re-approve.
    brand_kit_approved_at = db.Column(db.DateTime, nullable=True)

    # Business profile fields enriched via Tavily research — used by
    # the audit PDF's Business Profile table and Executive Summary.
    founded_year = db.Column(db.String(10), nullable=True)
    google_rating = db.Column(db.String(20), nullable=True)
    google_review_count = db.Column(db.Integer, nullable=True)
    business_summary = db.Column(db.Text, nullable=True)
    business_profile_updated_at = db.Column(db.DateTime, nullable=True)

    # When set, anyone with the URL /report/<public_share_token> can
    # view + export the latest audit PDF without logging in. Lets
    # agencies share polished reports with their clients without
    # creating user accounts. Owner can revoke at any time.
    public_share_token = db.Column(db.String(80), nullable=True, unique=True, index=True)
    public_share_created_at = db.Column(db.DateTime, nullable=True)

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


class CalComConnection(db.Model):
    """Key-based Cal.com booking connection scoped to a workspace.

    User generates an API key in Cal.com Settings → Developer and
    pastes it alongside their Cal.com username. Read-only — we list
    event types and recent bookings, never create/cancel anything.
    """
    __tablename__ = "calcom_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True
    )

    api_key = db.Column(db.Text, nullable=False)
    username = db.Column(db.String(120), nullable=False)
    last_payload = db.Column(db.JSON, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        db.UniqueConstraint("user_id", "client_id", name="uq_calcom_user_client"),
    )


class WooCommerceConnection(db.Model):
    """Key-based WooCommerce connection scoped to a workspace.

    The store admin generates a read-only Consumer Key + Consumer
    Secret in WooCommerce → Settings → Advanced → REST API and pastes
    them into our connect form. We never receive or store the WP
    admin password — only the scoped REST keys.
    """
    __tablename__ = "woocommerce_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True
    )

    store_url = db.Column(db.String(500), nullable=False)
    consumer_key = db.Column(db.Text, nullable=False)
    consumer_secret = db.Column(db.Text, nullable=False)

    last_audit_payload = db.Column(db.JSON, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        db.UniqueConstraint("user_id", "client_id", name="uq_woo_user_client"),
    )


class GoogleSearchConsoleConnection(db.Model):
    """OAuth-installed Google Search Console connection scoped to a
    workspace. Stores the long-lived refresh token so we can mint
    fresh access tokens when they expire (default ~1h validity).

    `site_url` is the verified Search Console property the user picked
    (e.g. "sc-domain:example.com" or "https://example.com/"). Cached
    KPI payload + last_synced_at let the dashboard render fast without
    re-hitting Google on every page view.
    """
    __tablename__ = "google_search_console_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True
    )

    site_url = db.Column(db.String(500), nullable=True)
    access_token = db.Column(db.Text, nullable=False)
    refresh_token = db.Column(db.Text, nullable=True)
    token_expires_at = db.Column(db.DateTime, nullable=True)
    scope = db.Column(db.Text, nullable=True)

    last_sync_payload = db.Column(db.JSON, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)

    # Google Analytics 4 reuses the same OAuth grant. ga_property_id is
    # the picked GA4 property (e.g. "123456789"); ga_payload caches the
    # 28-day summary for the dashboard.
    ga_property_id = db.Column(db.String(60), nullable=True)
    ga_payload = db.Column(db.JSON, nullable=True)
    ga_synced_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        db.UniqueConstraint("user_id", "client_id", name="uq_gsc_user_client"),
    )


class MarketplacePresence(db.Model):
    """A user's storefront on a third-party marketplace (Etsy, Amazon,
    Shopee, eBay). One row per (workspace, marketplace) — workspaces
    can have multiple presences across marketplaces.

    We don't ingest the catalog; the value is checking how the storefront
    surfaces in AI answers, similar to the AI Answer Monitor but with
    marketplace-flavoured prompts."""
    __tablename__ = "marketplace_presences"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True
    )

    # One of: etsy, amazon, shopee, ebay, other
    marketplace = db.Column(db.String(40), nullable=False)
    shop_name = db.Column(db.String(255), nullable=True)
    shop_url = db.Column(db.String(500), nullable=False)
    category = db.Column(db.String(120), nullable=True)
    region = db.Column(db.String(80), nullable=True)

    # Latest aggregate visibility from the most recent audit.
    last_visibility_score = db.Column(db.Integer, nullable=True)
    last_audit_payload = db.Column(db.JSON, nullable=True)
    last_audited_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PromptCheckSnapshot(db.Model):
    """Point-in-time result of running a tracked prompt against an AI
    answer engine. One row per check; the latest values also live on
    PromptTracking but the snapshot table is the source of truth for
    the trend line shown in the Answer Monitor."""
    __tablename__ = "prompt_check_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    prompt_tracking_id = db.Column(
        db.Integer,
        db.ForeignKey("prompt_tracking.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=True, index=True
    )

    engine = db.Column(db.String(60), nullable=True)
    brand_mentioned = db.Column(db.Boolean, default=False, nullable=False)
    brand_position = db.Column(db.Integer, nullable=True)
    score = db.Column(db.Integer, default=0, nullable=False)
    answer_type = db.Column(db.String(60), nullable=True)
    competitors_mentioned = db.Column(db.JSON, nullable=True)
    answer_excerpt = db.Column(db.Text, nullable=True)

    checked_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False, index=True
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


class ShopifyConnection(db.Model):
    """An OAuth-installed Shopify connection scoped to a workspace.

    The shop is where the user runs their store; the access_token is what
    we use to call Admin REST. Stored per (user, client) so different
    workspaces can be wired to different stores.
    """
    __tablename__ = "shopify_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True
    )

    # e.g. "my-store.myshopify.com"
    shop_domain = db.Column(db.String(255), nullable=False)

    # Access token from the OAuth callback. Stored as plain text — production
    # deployments should encrypt at rest (or move to a secrets manager).
    access_token = db.Column(db.Text, nullable=False)

    # Comma-separated scopes the token was granted with.
    scope = db.Column(db.Text, nullable=True)

    # Cached store metadata (name, country, plan, etc.).
    shop_meta = db.Column(db.JSON, nullable=True)

    # Last time we successfully synced products.
    last_synced_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        db.UniqueConstraint("user_id", "client_id", name="uq_shopify_user_client"),
    )


class BigCommerceConnection(db.Model):
    """A self-installed BigCommerce connection scoped to a workspace.

    Auth model is store_hash + access_token (X-Auth-Token). When we
    eventually publish a public app to the BigCommerce App Marketplace,
    add OAuth callback fields without disturbing this table.
    """
    __tablename__ = "bigcommerce_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True
    )
    store_hash = db.Column(db.String(120), nullable=False)
    access_token = db.Column(db.Text, nullable=False)
    store_meta = db.Column(db.JSON, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "client_id", name="uq_bigcommerce_user_client"
        ),
    )


class ShoplineConnection(db.Model):
    """A custom-app SHOPLINE connection scoped to a workspace.

    Auth model is store_handle (e.g. 'mystore' for mystore.myshopline.com)
    + access_token. Public-app OAuth flow is additive when we ship
    DarInsights as a SHOPLINE app.
    """
    __tablename__ = "shopline_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True
    )
    store_handle = db.Column(db.String(120), nullable=False)
    access_token = db.Column(db.Text, nullable=False)
    shop_meta = db.Column(db.JSON, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    __table_args__ = (
        db.UniqueConstraint("user_id", "client_id", name="uq_shopline_user_client"),
    )


class WixConnection(db.Model):
    """A Wix headless-API-key connection scoped to a workspace.

    Auth model is api_key + site_id. Switch to Wix App Marketplace
    OAuth when DarInsights is approved as a Wix app.
    """
    __tablename__ = "wix_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True
    )
    site_id = db.Column(db.String(120), nullable=False)
    api_key = db.Column(db.Text, nullable=False)
    site_meta = db.Column(db.JSON, nullable=True)
    # Cached collection list so the publish UI doesn't call the API on
    # every page render. Refreshed on demand from a settings button.
    collections_cache = db.Column(db.JSON, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    __table_args__ = (
        db.UniqueConstraint("user_id", "client_id", name="uq_wix_user_client"),
    )


class FramerConnection(db.Model):
    """A Framer PAT-based connection scoped to a workspace.

    Auth model is access_token + project_id. Framer's CMS API is in
    beta — surface stays narrow until they ship a stable update API.
    """
    __tablename__ = "framer_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True
    )
    project_id = db.Column(db.String(120), nullable=False)
    access_token = db.Column(db.Text, nullable=False)
    project_meta = db.Column(db.JSON, nullable=True)
    collections_cache = db.Column(db.JSON, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    __table_args__ = (
        db.UniqueConstraint("user_id", "client_id", name="uq_framer_user_client"),
    )


class SquarespaceConnection(db.Model):
    """A Squarespace API-key connection scoped to a workspace.

    Read-only by design: Squarespace's Content API only allows reads
    for pages/posts, and Commerce write endpoints are limited. Stays
    narrow until Squarespace ships a real CMS write API.
    """
    __tablename__ = "squarespace_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True
    )
    api_key = db.Column(db.Text, nullable=False)
    site_meta = db.Column(db.JSON, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "client_id", name="uq_squarespace_user_client"
        ),
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
        industry=client.get("industry", ""),
    )

    shopify_findings = _shopify_findings_for_client(user_id, client_id)
    if shopify_findings:
        actions = list(actions) + shopify_findings

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
    os.makedirs(app.config["AGENCY_LOGO_FOLDER"], exist_ok=True)


def agency_branding(user) -> Dict[str, Any]:
    """Resolve the agency-branding dict that PDFs and the in-app
    sidebar use. Always returns the same shape so templates don't
    need to special-case missing fields. Falls back to DarInsights
    branding when white-label is off."""
    from services.storage import logo_storage

    # Resolve the uploaded logo URL regardless of whether white-label
    # is active, so the Settings → White-label page can preview the
    # uploaded file even before the toggle is flipped.
    raw_logo_url = logo_storage.url(
        "agency_logos", getattr(user, "agency_logo_filename", None)
    ) if user else None

    if not user or not getattr(user, "is_white_label_enabled", False):
        return {
            "active": False,
            "name": "DarInsights",
            "tagline": "AI Visibility & Content Strategy",
            "website": None,
            # Surface the file even when not active so the upload card
            # can render its preview. The sidebar / PDF code paths
            # already gate on `active` before swapping the brand.
            "logo_url": raw_logo_url,
            "footer_text": None,
            "disclaimer": None,
        }
    logo_url = raw_logo_url
    return {
        "active": True,
        "name": user.agency_name or "Your Agency",
        "tagline": user.agency_tagline or "AI Visibility & Content Strategy",
        "website": user.agency_website,
        "logo_url": logo_url,
        "footer_text": user.agency_footer,
        "disclaimer": user.agency_disclaimer,
    }


def effective_agency_branding() -> Dict[str, Any]:
    """Resolve branding for the current request. Team members see the
    owner's agency branding so client-facing surfaces stay consistent."""
    if not current_user.is_authenticated:
        return agency_branding(None)
    target = effective_owner() or current_user
    return agency_branding(target)


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


def _workspace_logo_url(filename):
    """Resolve a workspace logo to its public URL (S3 or local)."""
    if not filename:
        return None
    from services.storage import logo_storage
    return logo_storage.url("workspace_logos", filename)


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
        "logo_url": _workspace_logo_url(client.logo_filename),
        "brand_kit": brand_kit_dict(client),
        "created_at": (
            client.created_at.isoformat() if client.created_at else None
        ),
        "updated_at": (
            client.updated_at.isoformat() if client.updated_at else None
        ),
    }


def brand_kit_dict(client) -> Dict[str, Any]:
    """Read the structured Brand Kit fields off a Client row. Used by
    serialize_client_row + downstream prompt-builders so generators
    have a stable shape to lift values from."""
    if not client:
        return {}
    return {
        "audience": getattr(client, "brand_audience", None),
        "services": getattr(client, "brand_services", None),
        "differentiators": getattr(client, "brand_differentiators", None),
        "voice": getattr(client, "brand_voice", None),
        "personality": getattr(client, "brand_personality", None),
        "avoid": getattr(client, "brand_avoid", None),
        "primary_color": getattr(client, "brand_primary_color", None),
        "secondary_color": getattr(client, "brand_secondary_color", None),
        "accent_color": getattr(client, "brand_accent_color", None),
        "typography": getattr(client, "brand_typography", None),
        "imagery_direction": getattr(client, "brand_imagery_direction", None),
        "updated_at": (
            client.brand_kit_updated_at.isoformat()
            if getattr(client, "brand_kit_updated_at", None) else None
        ),
        "approved_at": (
            client.brand_kit_approved_at.isoformat()
            if getattr(client, "brand_kit_approved_at", None) else None
        ),
    }


def brand_kit_context_block(client_or_dict) -> str:
    """Format the Brand Kit fields as a clean text block for prompts.

    Generators only lift the kit when it's been approved AND the
    approval timestamp is newer than the last edit — protects against
    half-formed direction baking into output. Returns empty string
    otherwise so callers can concat without checking for content."""
    approved_at = None
    updated_at = None
    if hasattr(client_or_dict, "brand_audience"):
        kit = brand_kit_dict(client_or_dict)
        approved_at = getattr(client_or_dict, "brand_kit_approved_at", None)
        updated_at = getattr(client_or_dict, "brand_kit_updated_at", None)
    elif isinstance(client_or_dict, dict):
        kit = client_or_dict.get("brand_kit") or {}
        approved_at = kit.get("approved_at")
        updated_at = kit.get("updated_at")
    else:
        kit = {}
    # Skip the block when the kit hasn't been approved yet, or when
    # the user edited after approving (clears approval automatically
    # in the save handler — but defensive double-check).
    if not approved_at:
        return ""
    if updated_at and approved_at and isinstance(approved_at, datetime) and isinstance(updated_at, datetime):
        if updated_at > approved_at:
            return ""
    fields = [
        ("Target audience", kit.get("audience")),
        ("Main offerings", kit.get("services")),
        ("What makes us different", kit.get("differentiators")),
        ("Voice / tone", kit.get("voice")),
        ("Personality", kit.get("personality")),
        ("Avoid", kit.get("avoid")),
        ("Imagery direction", kit.get("imagery_direction")),
    ]
    lines = [f"{label}: {value}" for label, value in fields if value]
    if not lines:
        return ""
    return "Brand Kit:\n" + "\n".join(lines)


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
    # Team members see the team owner's workspaces; solo users see their own.
    owner_id = effective_owner_id() or current_user.id
    clients = load_clients(user_id=owner_id)
    audits = get_saved_audits(user_id=owner_id)

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
            industry=client.get("industry", ""),
        )

        shopify_findings = _shopify_findings_for_client(
            current_user.id, client.get("id")
        )
        if shopify_findings:
            recommended_actions = list(recommended_actions) + shopify_findings

        # GSC + GA-driven recommendations. We read the cached payloads
        # the integration sync routes already write to the connection
        # row, so this stays HTTP-free on the render path.
        try:
            from services.google_data_recommendations import (
                build_google_data_recommendations,
            )
            gsc_conn = (
                GoogleSearchConsoleConnection.query.filter_by(
                    user_id=owner_id, client_id=client.get("id")
                ).first() if client.get("id") else None
            )
            if gsc_conn:
                google_recs = build_google_data_recommendations(
                    gsc_payload=gsc_conn.last_sync_payload,
                    ga_payload=gsc_conn.ga_payload,
                )
                if google_recs:
                    recommended_actions = list(recommended_actions) + google_recs
        except Exception as exc:
            logger.warning(
                "Google data recs failed for client %s: %s",
                client.get("id"), exc,
            )

        # Stale-content refresh recommendations — feeds the Growth
        # Calendar so it stays self-feeding between fresh audits.
        try:
            from services.stale_content import find_stale_actions
            wf_exports = (
                WebflowExport.query
                .filter_by(user_id=owner_id, client_id=client.get("id"))
                .all()
                if client.get("id") else []
            )
            client_queue = get_queue_items(
                client_id=client.get("id"),
                user_id=owner_id,
                include_dismissed=True,
            ) if client.get("id") else []
            stale = find_stale_actions(
                webflow_exports=wf_exports,
                queue_items=client_queue,
            )
            if stale:
                recommended_actions = list(recommended_actions) + stale
        except Exception as exc:
            logger.warning(
                "Stale-content scan failed for client %s: %s",
                client.get("id"), exc,
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
    Published/archived items do not count. Reads from PLAN_CATALOG so
    bumping a tier's limit is a one-line edit in pricing.py.
    """
    if not user:
        return 3
    if user_has_unlimited_credits(user):
        return 999
    plan = getattr(user, "plan", "free") or "free"
    return active_queue_limit_for_plan(plan)


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


def get_onboarding_state(user_id):
    """Three-step first-run flow state for the onboarding stepper.

    Steps:
      1. signup        — implicit, completed once user_id exists
      2. workspace     — has at least 1 workspace
      3. audit         — has at least 1 saved audit

    Returns a dict the templates render directly:
      {
        "active": bool,            # True until all 3 steps done
        "current_step": 1|2|3,     # the step the user is on now
        "steps": [
          {"key": "signup", "label": "Account", "done": True},
          {"key": "workspace", "label": "Workspace", "done": bool},
          {"key": "audit", "label": "First audit", "done": bool},
        ],
      }

    Cheap: workspace and audit lookups are already done elsewhere on
    every page render, so calling this in a template context isn't a
    new DB hit on hot paths."""
    if not user_id:
        return {"active": False, "current_step": 1, "steps": []}

    workspaces = load_clients(user_id=user_id)
    audits = get_saved_audits(user_id=user_id)

    has_workspace = bool(workspaces)
    has_audit = bool(audits)

    if not has_workspace:
        current_step = 2
    elif not has_audit:
        current_step = 3
    else:
        current_step = 3  # done — stepper inactive

    return {
        "active": not (has_workspace and has_audit),
        "current_step": current_step,
        "steps": [
            {"key": "signup", "label": "Account", "done": True},
            {"key": "workspace", "label": "Workspace", "done": has_workspace},
            {"key": "audit", "label": "First audit", "done": has_audit},
        ],
    }


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
    # Team members spend from the owner's wallet — billing is a team
    # property, not a per-member one.
    if user and getattr(user, "team_owner_id", None):
        owner = User.query.get(user.team_owner_id)
        if owner:
            user = owner
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


def effective_owner_id(user=None) -> Optional[int]:
    """Return the User.id of the account that owns this user's data.

    For team members (team_owner_id set) this is the inviter; for solo
    users it's the user themselves. Used everywhere queries need to
    span "the team's" data without leaking a member's view of an owner
    they don't belong to."""
    target = user if user is not None else current_user
    if not target or not target.is_authenticated:
        return None
    return getattr(target, "team_owner_id", None) or target.id


def effective_owner(user=None):
    """Return the owning User row (the inviter for team members, or
    the user themselves)."""
    target = user if user is not None else current_user
    if not target or not target.is_authenticated:
        return None
    if getattr(target, "team_owner_id", None):
        return User.query.get(target.team_owner_id)
    return target


def count_team_members(owner_id: int) -> int:
    """Members + pending invites currently consuming seats for this owner."""
    members = User.query.filter_by(team_owner_id=owner_id).count()
    pending = TeamInvite.query.filter_by(
        owner_user_id=owner_id, status="pending"
    ).count()
    # +1 for the owner themselves.
    return members + pending + 1


def get_seat_limit(user) -> int:
    """Plan base seat cap + extra seats purchased."""
    if not user:
        return 1
    if user.role == "admin" or user.plan == "dev_unlimited":
        return 999
    from pricing import seat_limit_for_plan as _seat
    base = _seat(getattr(user, "plan", "free") or "free")
    extras = int(getattr(user, "extra_seats", 0) or 0)
    return base + extras


def user_active_modules(user) -> set[str]:
    """Module slugs this user can access right now.

    Union of two sources:
    1. Explicit UserModule rows with status='active' (modules system).
    2. Implicit modules unlocked by the user's legacy plan tier
       (so existing pro/growth customers don't regress while modules
       stay dark in the dashboard).

    See modules.py for the catalog and PLAN_IMPLICIT_MODULES map.
    """
    if not user:
        return set()
    from modules import implicit_modules_for_plan
    implicit = set(implicit_modules_for_plan(getattr(user, "plan", None)))
    explicit_rows = (
        UserModule.query
        .filter_by(user_id=user.id, status="active")
        .all()
    )
    explicit = {row.module_slug for row in explicit_rows}
    return implicit | explicit


def user_has_module(user, module_slug: str) -> bool:
    """True if `user` has access to `module_slug` (explicit or implicit)."""
    return (module_slug or "").lower() in user_active_modules(user)


def user_has_feature(user, feature_slug: str) -> bool:
    """True if `user` has access to the module that owns `feature_slug`.
    Returns False for unknown features — feature gates fail closed."""
    from modules import module_for_feature
    module = module_for_feature(feature_slug)
    if not module:
        return False
    return user_has_module(user, module)


def grant_monthly_credits_if_due(user) -> int:
    """If `user` is on a paid plan and at least 28 days have passed since
    their last monthly_allowance grant (or they've never received one),
    credit their wallet with the plan's allowance and record the
    transaction. Returns the number of credits granted (0 if not due)."""
    if not user or not getattr(user, "plan", None):
        return 0
    monthly = monthly_credit_allowance(user.plan)
    if monthly <= 0:
        return 0

    last_grant = (
        CreditTransaction.query
        .filter_by(user_id=user.id, type="monthly_allowance")
        .order_by(CreditTransaction.created_at.desc())
        .first()
    )
    now = datetime.utcnow()
    if last_grant and (now - last_grant.created_at).days < 28:
        return 0

    if not user.wallet:
        user.wallet = Wallet(user_id=user.id, balance=0)
        db.session.add(user.wallet)
        db.session.flush()

    user.wallet.balance += monthly
    db.session.add(
        CreditTransaction(
            user_id=user.id,
            type="monthly_allowance",
            amount=monthly,
            balance_after=user.wallet.balance,
            notes=f"Monthly allowance — {get_plan(user.plan).get('label', user.plan)} plan",
        )
    )
    db.session.commit()
    return monthly


@app.before_request
def _maybe_grant_monthly_credits():
    """Top up paid users with their monthly allowance once per period."""
    if not current_user.is_authenticated:
        return
    # Cheap fast-path: skip if the request is for static assets.
    if request.endpoint and request.endpoint.startswith("static"):
        return
    try:
        grant_monthly_credits_if_due(current_user)
    except Exception as exc:
        logger.warning("Monthly credit grant failed for user %s: %s", current_user.id, exc)


def has_enough_credits_for(user, action_key: str) -> bool:
    """Convenience: does the user have enough credits for a named action?"""
    from pricing import get_action_cost
    return has_enough_credits(user, get_action_cost(action_key))


def spend_credits_for(user, action_key: str, notes: str = "") -> bool:
    """Deduct the canonical credit cost for an action. Returns False
    if the wallet doesn't have the funds (caller should flash + bail)."""
    from pricing import get_action_cost
    cost = get_action_cost(action_key)
    if cost <= 0:
        return True
    return spend_credits(
        user,
        cost,
        tx_type=f"usage_{action_key}",
        notes=notes or action_key.replace("_", " ").title(),
    )


def safe_return_url(candidate: str) -> str | None:
    """Validate a `return_to` URL coming from a query parameter or
    session value. Only allows same-origin paths (starting with /
    and not //) so a malicious link can't redirect users to an
    external site after checkout. Returns None for anything
    suspicious; callers should fall back to a known-good default."""
    if not candidate or not isinstance(candidate, str):
        return None
    candidate = candidate.strip()
    # Must start with single / (server-relative path), not // (protocol-
    # relative which can hop to another origin).
    if not candidate.startswith("/") or candidate.startswith("//"):
        return None
    # Reject anything containing a scheme or @ (defence in depth).
    if "://" in candidate or candidate.startswith("/\\"):
        return None
    return candidate


def pricing_redirect_with_return_to():
    """Build a `redirect(url_for('pricing_page', return_to=...))`
    so /pricing can capture the return URL into the session and
    /stripe/success can hop the user back to where they were
    interrupted. Use this anywhere we'd otherwise just call
    `redirect(url_for('pricing_page'))` mid-flow."""
    target = safe_return_url(request.path)
    if not target:
        return redirect(url_for("pricing_page"))
    return redirect(url_for("pricing_page", return_to=target))


def insufficient_credits_message(user, action_key: str, action_label: str) -> str:
    """Build a richer 'you don't have enough credits' message that
    actually tells the user the cost vs. their balance, so they
    know whether to top up or upgrade. Used by every credit-gated
    action route just before redirecting to /pricing."""
    from pricing import get_action_cost
    cost = get_action_cost(action_key)

    balance = 0
    if user_has_unlimited_credits(user):
        balance = "Unlimited"
    elif user and getattr(user, "wallet", None):
        balance = user.wallet.balance

    if isinstance(balance, int) and balance < cost:
        short_by = cost - balance
        return (
            f"{action_label} costs {cost} credit"
            f"{'s' if cost != 1 else ''}, you have {balance}. "
            f"Top up {short_by} credit{'s' if short_by != 1 else ''} or upgrade your plan to continue."
        )
    return (
        f"Couldn't deduct {cost} credit{'s' if cost != 1 else ''} for {action_label.lower()}. "
        "Your balance may have changed mid-request — try again."
    )


def has_enough_credits(user, amount):
    if user_has_unlimited_credits(user):
        return True
    if not user:
        return False
    # Check the owner's wallet for team members.
    if getattr(user, "team_owner_id", None):
        owner = User.query.get(user.team_owner_id)
        if not owner or not owner.wallet:
            return False
        return owner.wallet.balance >= amount
    if not user.wallet:
        return False
    return user.wallet.balance >= amount


REFERRAL_WINDOW_DAYS = 30
REFERRAL_PAYOUT_RATE = 0.25  # 25% of paid value


def award_referral_for_payment(
    *,
    referred_user,
    amount_usd: float,
    stripe_event_ref: str = "",
) -> bool:
    """Apply the 25% referral reward when the referred user makes a
    paid purchase within the 30-day window from their signup.

    Returns True when a reward was granted, False otherwise. Every
    qualifying payment creates its own Referral row, so a referrer
    keeps earning on each subsequent purchase the referred user makes
    inside the window (subscriptions + credit bundles)."""
    if not referred_user:
        return False
    if not getattr(referred_user, "referred_by_user_id", None):
        return False
    if amount_usd is None or amount_usd <= 0:
        return False

    # Window check — measured from the referred user's signup.
    signed_up_at = getattr(referred_user, "created_at", None)
    if signed_up_at and (datetime.utcnow() - signed_up_at).days >= REFERRAL_WINDOW_DAYS:
        return False

    # Idempotency — never reward the same Stripe session twice.
    if stripe_event_ref:
        already = Referral.query.filter_by(
            stripe_event_ref=stripe_event_ref
        ).first()
        if already:
            return False

    referrer = User.query.get(referred_user.referred_by_user_id)
    if not referrer:
        return False
    if not referrer.wallet:
        referrer.wallet = Wallet(user_id=referrer.id, balance=0)
        db.session.add(referrer.wallet)
        db.session.flush()

    # 1 credit ≈ $1 of value for subscribers, $0.50 for free-plan
    # users at the topup-bundle level. The referrer's reward is
    # measured in credits (1 credit = $1 = direct offset), regardless
    # of the referred user's plan tier — they earn what they pay.
    reward_credits = max(1, int(round(amount_usd * REFERRAL_PAYOUT_RATE)))

    referrer.wallet.balance += reward_credits
    db.session.add(
        CreditTransaction(
            user_id=referrer.id,
            type="referral_bonus",
            amount=reward_credits,
            balance_after=referrer.wallet.balance,
            notes=(
                f"Referral reward (25% of ${amount_usd:.2f} paid by "
                f"{referred_user.email})"
            ),
        )
    )
    db.session.add(
        Referral(
            referrer_user_id=referrer.id,
            referred_user_id=referred_user.id,
            referral_code=referrer.referral_code or "",
            status="rewarded",
            referred_payment_usd=round(amount_usd, 2),
            reward_credits_referrer=reward_credits,
            stripe_event_ref=stripe_event_ref or None,
            qualified_at=datetime.utcnow(),
            rewarded_at=datetime.utcnow(),
        )
    )
    db.session.commit()
    return True


def award_referral_if_qualified(user):
    """Backward-compat shim — old callers still expect this name.
    The new model only awards on paid purchases, so a non-payment
    signup hook is a no-op now."""
    return False


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


def _migrate_csv_interest_to_db():
    """One-shot migration: read data/interest_waitlist.csv (if it exists)
    and copy any rows that aren't yet in the DB. Idempotent — keyed on
    email + created_at — so it's safe to call repeatedly during the
    transition. Renames the CSV to .migrated when done so we don't
    re-process it."""
    file_path = "data/interest_waitlist.csv"
    if not os.path.exists(file_path):
        return
    try:
        existing_emails = {
            (s.email or "").lower()
            for s in InterestSignup.query.with_entities(InterestSignup.email).all()
        }
        with open(file_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                email = (row.get("email") or "").strip().lower()
                if not email or email in existing_emails:
                    continue
                created_at = None
                try:
                    created_at = datetime.fromisoformat(row.get("created_at") or "")
                except Exception:
                    created_at = datetime.utcnow()
                signup = InterestSignup(
                    email=email,
                    company=(row.get("company") or "").strip() or None,
                    use_case=(row.get("use_case") or "").strip() or None,
                    status="new",
                    source="landing-csv-import",
                    created_at=created_at or datetime.utcnow(),
                )
                db.session.add(signup)
                existing_emails.add(email)
            db.session.commit()
        os.rename(file_path, file_path + ".migrated")
    except Exception as exc:
        logger.warning("CSV → DB interest migration failed: %s", exc)
        db.session.rollback()


@app.route("/interest", methods=["POST"])
def collect_interest():
    email = (request.form.get("email") or "").strip().lower()
    company = (request.form.get("company") or "").strip()
    use_case = (request.form.get("use_case") or "").strip()

    if not email:
        return redirect("/?interest=missing#early-access")

    # Pull any pre-existing CSV waitlist into the DB on first call.
    _migrate_csv_interest_to_db()

    # Capture marketing attribution + referrer for the CRM, falling
    # back to the request when no UTM params are present.
    utm_source = (request.values.get("utm_source") or "").strip()[:120] or None
    utm_medium = (request.values.get("utm_medium") or "").strip()[:120] or None
    utm_campaign = (request.values.get("utm_campaign") or "").strip()[:120] or None
    referrer = (request.referrer or "")[:500] or None
    user_agent = (request.headers.get("User-Agent") or "")[:500] or None

    # Re-signup: update the existing row (don't duplicate) but bump
    # updated_at so the admin sees the latest activity. Status stays
    # "new" only if it was new before; otherwise leave it as-is so
    # the admin's prior triage isn't reverted.
    existing = (
        InterestSignup.query
        .filter(db.func.lower(InterestSignup.email) == email)
        .first()
    )
    if existing:
        existing.company = company or existing.company
        existing.use_case = use_case or existing.use_case
        existing.utm_source = utm_source or existing.utm_source
        existing.utm_medium = utm_medium or existing.utm_medium
        existing.utm_campaign = utm_campaign or existing.utm_campaign
        existing.referrer = referrer or existing.referrer
        existing.user_agent = user_agent or existing.user_agent
        # No status change — admin's existing triage wins.
    else:
        db.session.add(InterestSignup(
            email=email,
            company=company or None,
            use_case=use_case or None,
            source="landing",
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            referrer=referrer,
            user_agent=user_agent,
        ))
    db.session.commit()

    return redirect("/?interest=success#early-access")


# ---------------------------------------------------------------------------
# Interest CRM
# ---------------------------------------------------------------------------
# Simple admin-only CRM for the landing-page interest list.
#
# Surfaces:
#   GET  /admin/interest                    list + filters + stats
#   POST /admin/interest/<id>/status        update status
#   POST /admin/interest/<id>/notes         save notes / tags
#   POST /admin/interest/<id>/delete        soft delete (status=removed)
#   GET  /admin/interest/export.csv         export filtered list
#
# Access control: User.role == "admin". Anyone else gets 403.

INTEREST_STATUSES = [
    ("new", "New"),
    ("contacted", "Contacted"),
    ("replied", "Replied"),
    ("converted", "Converted"),
    ("declined", "Declined"),
    ("unsubscribed", "Unsubscribed"),
]


def _require_admin():
    """Returns None when the request should proceed, or a (response,
    status) tuple to short-circuit."""
    if not current_user.is_authenticated:
        return redirect(url_for("login", next=request.path)), 302
    if (current_user.role or "user").lower() != "admin":
        abort(403)
    return None


@app.route("/admin/interest", methods=["GET"])
@login_required
def admin_interest_list():
    guard = _require_admin()
    if guard is not None:
        return guard[0]

    # Pull any legacy CSV rows into the DB before listing.
    _migrate_csv_interest_to_db()

    q = (request.args.get("q") or "").strip().lower()
    status_filter = (request.args.get("status") or "").strip().lower()

    rows = InterestSignup.query
    if q:
        like = f"%{q}%"
        rows = rows.filter(
            db.or_(
                InterestSignup.email.ilike(like),
                InterestSignup.company.ilike(like),
                InterestSignup.notes.ilike(like),
            )
        )
    if status_filter and status_filter in dict(INTEREST_STATUSES):
        rows = rows.filter(InterestSignup.status == status_filter)
    rows = rows.order_by(InterestSignup.created_at.desc()).all()

    # Aggregates for the stats strip.
    all_signups = InterestSignup.query.all()
    total = len(all_signups)
    by_status = {key: 0 for key, _ in INTEREST_STATUSES}
    for s in all_signups:
        if s.status in by_status:
            by_status[s.status] += 1
    week_ago = datetime.utcnow() - timedelta(days=7)
    new_this_week = sum(1 for s in all_signups if s.created_at and s.created_at >= week_ago)
    use_case_counts: Dict[str, int] = {}
    for s in all_signups:
        key = (s.use_case or "Unspecified").strip() or "Unspecified"
        use_case_counts[key] = use_case_counts.get(key, 0) + 1

    return render_template(
        "admin/interest_list.html",
        rows=rows,
        total=total,
        by_status=by_status,
        new_this_week=new_this_week,
        use_case_counts=use_case_counts,
        statuses=INTEREST_STATUSES,
        active_status=status_filter,
        active_query=request.args.get("q") or "",
    )


@app.route("/admin/interest/<int:signup_id>/status", methods=["POST"])
@login_required
def admin_interest_update_status(signup_id):
    guard = _require_admin()
    if guard is not None:
        return guard[0]
    signup = db.session.get(InterestSignup, signup_id)
    if not signup:
        abort(404)
    new_status = (request.form.get("status") or "").strip().lower()
    if new_status not in dict(INTEREST_STATUSES):
        flash(f"Unknown status: {new_status}", "error")
        return redirect(url_for("admin_interest_list"))
    signup.status = new_status
    if new_status == "contacted" and not signup.last_contacted_at:
        signup.last_contacted_at = datetime.utcnow()
    if new_status == "converted" and not signup.converted_at:
        signup.converted_at = datetime.utcnow()
    db.session.commit()
    flash(f"Status updated to {new_status}.", "success")
    return redirect(url_for("admin_interest_list", **request.args.to_dict()))


@app.route("/admin/interest/<int:signup_id>/notes", methods=["POST"])
@login_required
def admin_interest_update_notes(signup_id):
    guard = _require_admin()
    if guard is not None:
        return guard[0]
    signup = db.session.get(InterestSignup, signup_id)
    if not signup:
        abort(404)
    signup.notes = (request.form.get("notes") or "").strip() or None
    tags_raw = (request.form.get("tags") or "").strip()
    if tags_raw:
        signup.tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    else:
        signup.tags = None
    db.session.commit()
    flash("Notes saved.", "success")
    return redirect(url_for("admin_interest_list", **request.args.to_dict()))


@app.route("/admin/interest/<int:signup_id>/delete", methods=["POST"])
@login_required
def admin_interest_delete(signup_id):
    guard = _require_admin()
    if guard is not None:
        return guard[0]
    signup = db.session.get(InterestSignup, signup_id)
    if signup:
        db.session.delete(signup)
        db.session.commit()
        flash("Signup removed.", "success")
    return redirect(url_for("admin_interest_list", **request.args.to_dict()))


@app.route("/admin", methods=["GET"])
@login_required
def admin_overview():
    """Admin overview — KPI strip + recent activity across the app."""
    guard = _require_admin()
    if guard is not None:
        return guard[0]

    week_ago = datetime.utcnow() - timedelta(days=7)

    # Headline counts.
    total_users = User.query.count()
    new_users_week = User.query.filter(User.created_at >= week_ago).count()
    paying_users = User.query.filter(User.plan.in_(["pro", "growth"])).count()
    total_clients = Client.query.count()
    total_signups = InterestSignup.query.count()
    new_signups_week = InterestSignup.query.filter(
        InterestSignup.created_at >= week_ago
    ).count()
    total_campaigns = EmailCampaign.query.count()
    sent_emails_week = (
        EmailCampaignRecipient.query
        .filter(
            EmailCampaignRecipient.status == "sent",
            EmailCampaignRecipient.sent_at >= week_ago,
        )
        .count()
    )

    # Wallet totals.
    wallet_balance_total = (
        db.session.query(db.func.coalesce(db.func.sum(Wallet.balance), 0)).scalar() or 0
    )

    # Recent activity feeds (5 each).
    recent_signups = (
        InterestSignup.query.order_by(InterestSignup.created_at.desc()).limit(5).all()
    )
    recent_users = (
        User.query.order_by(User.created_at.desc()).limit(5).all()
    )
    recent_campaigns = (
        EmailCampaign.query.order_by(EmailCampaign.created_at.desc()).limit(5).all()
    )

    return render_template(
        "admin/overview.html",
        total_users=total_users,
        new_users_week=new_users_week,
        paying_users=paying_users,
        total_clients=total_clients,
        total_signups=total_signups,
        new_signups_week=new_signups_week,
        total_campaigns=total_campaigns,
        sent_emails_week=sent_emails_week,
        wallet_balance_total=wallet_balance_total,
        recent_signups=recent_signups,
        recent_users=recent_users,
        recent_campaigns=recent_campaigns,
    )


@app.route("/admin/users", methods=["GET"])
@login_required
def admin_users_list():
    """Searchable user list. One row per User; click through to detail."""
    guard = _require_admin()
    if guard is not None:
        return guard[0]

    q = (request.args.get("q") or "").strip().lower()
    plan_filter = (request.args.get("plan") or "").strip().lower()

    rows = User.query
    if q:
        like = f"%{q}%"
        rows = rows.filter(db.or_(User.email.ilike(like), User.name.ilike(like)))
    if plan_filter:
        rows = rows.filter(User.plan == plan_filter)
    rows = rows.order_by(User.created_at.desc()).all()

    # Per-user metadata: workspace + audit counts. Cheap loop because
    # this is admin-only and N is small.
    summaries = []
    for u in rows:
        wallet = u.wallet
        summaries.append({
            "user": u,
            "wallet_balance": wallet.balance if wallet else 0,
            "workspace_count": Client.query.filter_by(user_id=u.id).count(),
            "credit_tx_count": CreditTransaction.query.filter_by(user_id=u.id).count(),
        })

    return render_template(
        "admin/users_list.html",
        summaries=summaries,
        active_query=request.args.get("q") or "",
        active_plan=plan_filter,
        plans=["free", "pro", "growth", "starter", "agency", "dev_unlimited"],
    )


@app.route("/admin/users/<int:user_id>", methods=["GET"])
@login_required
def admin_user_detail(user_id):
    guard = _require_admin()
    if guard is not None:
        return guard[0]
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    workspaces = Client.query.filter_by(user_id=user_id).order_by(Client.created_at.desc()).all()
    transactions = (
        CreditTransaction.query
        .filter_by(user_id=user_id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(50)
        .all()
    )

    return render_template(
        "admin/user_detail.html",
        u=user,
        wallet=user.wallet,
        workspaces=workspaces,
        transactions=transactions,
        plans=["free", "pro", "growth", "starter", "agency", "dev_unlimited"],
        roles=["user", "admin"],
    )


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@login_required
def admin_user_set_role(user_id):
    guard = _require_admin()
    if guard is not None:
        return guard[0]
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    new_role = (request.form.get("role") or "").strip().lower()
    if new_role not in {"user", "admin"}:
        flash(f"Unknown role: {new_role}", "error")
        return redirect(url_for("admin_user_detail", user_id=user_id))
    user.role = new_role
    db.session.commit()
    flash(f"Role updated to {new_role}.", "success")
    return redirect(url_for("admin_user_detail", user_id=user_id))


@app.route("/admin/users/<int:user_id>/plan", methods=["POST"])
@login_required
def admin_user_set_plan(user_id):
    guard = _require_admin()
    if guard is not None:
        return guard[0]
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    new_plan = (request.form.get("plan") or "").strip().lower()
    if new_plan not in PLAN_CATALOG:
        flash(f"Unknown plan: {new_plan}", "error")
        return redirect(url_for("admin_user_detail", user_id=user_id))
    user.plan = new_plan
    db.session.commit()
    flash(f"Plan updated to {new_plan}.", "success")
    return redirect(url_for("admin_user_detail", user_id=user_id))


@app.route("/admin/users/<int:user_id>/grant-credits", methods=["POST"])
@login_required
def admin_user_grant_credits(user_id):
    guard = _require_admin()
    if guard is not None:
        return guard[0]
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    try:
        amount = int(request.form.get("amount") or "0")
    except ValueError:
        flash("Amount must be an integer.", "error")
        return redirect(url_for("admin_user_detail", user_id=user_id))
    if amount == 0:
        return redirect(url_for("admin_user_detail", user_id=user_id))

    if not user.wallet:
        user.wallet = Wallet(user_id=user.id, balance=0)
        db.session.add(user.wallet)
        db.session.flush()
    user.wallet.balance += amount
    note = (request.form.get("note") or "").strip() or "Admin grant"
    db.session.add(CreditTransaction(
        user_id=user.id,
        type="admin_grant",
        amount=amount,
        balance_after=user.wallet.balance,
        notes=note,
    ))
    db.session.commit()
    flash(f"Granted {amount} credits.", "success")
    return redirect(url_for("admin_user_detail", user_id=user_id))


@app.route("/admin/payments", methods=["GET"])
@login_required
def admin_payments():
    """Payments + active subscriptions. Pulls live data from Stripe
    when configured; falls back to local CreditTransaction history."""
    guard = _require_admin()
    if guard is not None:
        return guard[0]

    charges = []
    subscriptions = []
    stripe_error = None
    try:
        from services.stripe_helper import is_stripe_configured, _stripe_module
        if is_stripe_configured():
            stripe = _stripe_module()
            charges = list(
                stripe.Charge.list(limit=50).data
            )
            subscriptions = list(
                stripe.Subscription.list(status="active", limit=50, expand=["data.customer"]).data
            )
    except Exception as exc:
        stripe_error = str(exc)[:300]

    # Local credit transactions (last 50) — useful even when Stripe
    # isn't configured.
    recent_tx = (
        CreditTransaction.query
        .order_by(CreditTransaction.created_at.desc())
        .limit(50)
        .all()
    )

    # MRR estimate from active Stripe subs.
    mrr_cents = 0
    for sub in subscriptions:
        for item in (sub.get("items", {}).get("data") or []):
            price = item.get("price") or {}
            unit_amount = price.get("unit_amount") or 0
            interval = ((price.get("recurring") or {}).get("interval") or "month").lower()
            qty = item.get("quantity") or 1
            if interval == "year":
                mrr_cents += int(unit_amount * qty / 12)
            else:
                mrr_cents += int(unit_amount * qty)

    return render_template(
        "admin/payments.html",
        charges=charges,
        subscriptions=subscriptions,
        recent_tx=recent_tx,
        mrr=round(mrr_cents / 100, 2),
        stripe_error=stripe_error,
    )


# ---------- Email campaigns ----------

@app.route("/admin/campaigns", methods=["GET"])
@login_required
def admin_campaigns_list():
    guard = _require_admin()
    if guard is not None:
        return guard[0]
    rows = (
        EmailCampaign.query
        .order_by(EmailCampaign.created_at.desc())
        .all()
    )
    return render_template("admin/campaigns_list.html", rows=rows)


@app.route("/admin/campaigns/new", methods=["GET", "POST"])
@login_required
def admin_campaign_new():
    guard = _require_admin()
    if guard is not None:
        return guard[0]

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        subject = (request.form.get("subject") or "").strip()
        body_html = (request.form.get("body_html") or "").strip()
        body_text = (request.form.get("body_text") or "").strip()
        audience_source = (request.form.get("audience_source") or "interest").strip()

        if not name or not subject or (not body_html and not body_text):
            flash("Name, subject, and a body (HTML or text) are required.", "error")
            return redirect(url_for("admin_campaign_new"))

        # Build audience filter dict from form.
        audience_filter: Dict[str, Any] = {"source": audience_source}
        if audience_source == "interest":
            sel_status = request.form.getlist("interest_status")
            if sel_status:
                audience_filter["status"] = sel_status
        elif audience_source == "users":
            sel_plan = request.form.getlist("user_plan")
            if sel_plan:
                audience_filter["plan"] = sel_plan
        elif audience_source == "manual":
            emails_raw = (request.form.get("manual_emails") or "")
            audience_filter["emails"] = [
                e.strip().lower() for e in emails_raw.replace(",", "\n").splitlines()
                if e.strip()
            ]

        campaign = EmailCampaign(
            name=name,
            subject=subject,
            body_html=body_html or None,
            body_text=body_text or None,
            audience_filter=audience_filter,
            sender_email=(request.form.get("sender_email") or "").strip() or None,
            status="draft",
            created_by_user_id=current_user.id,
        )
        db.session.add(campaign)
        db.session.commit()
        flash("Campaign drafted. Preview it before sending.", "success")
        return redirect(url_for("admin_campaign_detail", campaign_id=campaign.id))

    return render_template("admin/campaign_new.html")


@app.route("/admin/campaigns/<int:campaign_id>", methods=["GET"])
@login_required
def admin_campaign_detail(campaign_id):
    guard = _require_admin()
    if guard is not None:
        return guard[0]
    campaign = db.session.get(EmailCampaign, campaign_id)
    if not campaign:
        abort(404)

    audience = _resolve_campaign_audience(campaign.audience_filter or {})
    recipients = (
        EmailCampaignRecipient.query
        .filter_by(campaign_id=campaign.id)
        .order_by(EmailCampaignRecipient.created_at.desc())
        .all()
    )

    return render_template(
        "admin/campaign_detail.html",
        campaign=campaign,
        audience_size=len(audience),
        audience_preview=audience[:10],
        recipients=recipients,
    )


def _resolve_campaign_audience(filt: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Turn a campaign's audience_filter into a flat list of
    {email, source, source_id} dicts. Deduped by lowercase email."""
    source = (filt or {}).get("source") or "interest"
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _add(email: str, src: str, src_id):
        e = (email or "").strip().lower()
        if not e or e in seen:
            return
        seen.add(e)
        out.append({"email": e, "source": src, "source_id": src_id})

    if source == "interest":
        rows = InterestSignup.query
        statuses = filt.get("status") or []
        if statuses:
            rows = rows.filter(InterestSignup.status.in_(statuses))
        # Skip unsubscribed unless explicitly included.
        if not statuses or "unsubscribed" not in statuses:
            rows = rows.filter(InterestSignup.status != "unsubscribed")
        for r in rows.all():
            _add(r.email, "interest", r.id)
    elif source == "users":
        rows = User.query
        plans = filt.get("plan") or []
        if plans:
            rows = rows.filter(User.plan.in_(plans))
        for u in rows.all():
            _add(u.email, "user", u.id)
    elif source == "manual":
        for e in (filt.get("emails") or []):
            _add(e, "manual", None)
    return out


@app.route("/admin/campaigns/<int:campaign_id>/send", methods=["POST"])
@login_required
def admin_campaign_send(campaign_id):
    """Send a campaign. Iterates the resolved audience, calls the
    email helper for each, persists per-recipient status. Synchronous
    for simplicity — fine for the scale of an interest list. Wrap in
    a background job when audience > a few hundred."""
    guard = _require_admin()
    if guard is not None:
        return guard[0]
    campaign = db.session.get(EmailCampaign, campaign_id)
    if not campaign:
        abort(404)
    if campaign.status in ("sending", "sent"):
        flash(f"Campaign is already {campaign.status}.", "info")
        return redirect(url_for("admin_campaign_detail", campaign_id=campaign.id))

    from services.email_helper import is_email_configured, send_email
    if not is_email_configured():
        flash("Email isn't configured (set RESEND_API_KEY or SMTP_*).", "error")
        return redirect(url_for("admin_campaign_detail", campaign_id=campaign.id))

    audience = _resolve_campaign_audience(campaign.audience_filter or {})
    if not audience:
        flash("No recipients matched the audience filter.", "error")
        return redirect(url_for("admin_campaign_detail", campaign_id=campaign.id))

    campaign.status = "sending"
    db.session.commit()

    sent = failed = 0
    for r in audience:
        existing = (
            EmailCampaignRecipient.query
            .filter_by(campaign_id=campaign.id, email=r["email"])
            .first()
        )
        if existing and existing.status == "sent":
            continue
        recipient = existing or EmailCampaignRecipient(
            campaign_id=campaign.id,
            email=r["email"],
            source=r["source"],
            source_id=r["source_id"],
        )
        if not existing:
            db.session.add(recipient)

        ok = False
        try:
            ok = send_email(
                to=r["email"],
                subject=campaign.subject,
                body_text=campaign.body_text or (campaign.body_html or "")[:5000],
                body_html=campaign.body_html or None,
            )
        except Exception as exc:
            recipient.error = str(exc)[:500]

        recipient.status = "sent" if ok else "failed"
        recipient.sent_at = datetime.utcnow() if ok else None
        if ok:
            sent += 1
        else:
            failed += 1

    campaign.sent_count = sent
    campaign.failed_count = failed
    campaign.sent_at = datetime.utcnow()
    campaign.status = "sent" if failed == 0 else ("sent" if sent > 0 else "failed")
    db.session.commit()

    flash(f"Sent {sent} email(s); {failed} failed.", "success" if failed == 0 else "warning")
    return redirect(url_for("admin_campaign_detail", campaign_id=campaign.id))


@app.route("/admin/campaigns/<int:campaign_id>/preview", methods=["POST"])
@login_required
def admin_campaign_preview(campaign_id):
    """Send the campaign body to the admin's own email as a preview."""
    guard = _require_admin()
    if guard is not None:
        return guard[0]
    campaign = db.session.get(EmailCampaign, campaign_id)
    if not campaign:
        abort(404)
    from services.email_helper import is_email_configured, send_email
    if not is_email_configured():
        flash("Email isn't configured.", "error")
        return redirect(url_for("admin_campaign_detail", campaign_id=campaign.id))
    ok = send_email(
        to=current_user.email,
        subject=f"[PREVIEW] {campaign.subject}",
        body_text=campaign.body_text or (campaign.body_html or "")[:5000],
        body_html=campaign.body_html or None,
    )
    flash(
        f"Preview sent to {current_user.email}." if ok else "Preview send failed.",
        "success" if ok else "error",
    )
    return redirect(url_for("admin_campaign_detail", campaign_id=campaign.id))


@app.route("/admin/interest/export.csv", methods=["GET"])
@login_required
def admin_interest_export():
    guard = _require_admin()
    if guard is not None:
        return guard[0]

    rows = InterestSignup.query.order_by(InterestSignup.created_at.desc()).all()

    import io as _io
    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "email", "company", "use_case", "status", "notes", "tags",
        "source", "utm_source", "utm_medium", "utm_campaign", "referrer",
        "last_contacted_at", "converted_at", "created_at", "updated_at",
    ])
    for r in rows:
        writer.writerow([
            r.id, r.email, r.company or "", r.use_case or "", r.status,
            (r.notes or "").replace("\n", " "),
            ",".join(r.tags or []),
            r.source or "", r.utm_source or "", r.utm_medium or "",
            r.utm_campaign or "", r.referrer or "",
            r.last_contacted_at.isoformat() if r.last_contacted_at else "",
            r.converted_at.isoformat() if r.converted_at else "",
            r.created_at.isoformat() if r.created_at else "",
            r.updated_at.isoformat() if r.updated_at else "",
        ])

    response = make_response(buf.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="interest-signups-{datetime.utcnow().strftime("%Y%m%d")}.csv"'
    )
    return response


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
def pricing_page():
    """Public pricing page — renders the plan catalog."""
    from services.stripe_helper import is_stripe_configured

    current_plan = (
        getattr(current_user, "plan", None)
        if current_user.is_authenticated
        else None
    )
    # ?canceled=1 is set by the Stripe Checkout cancel_url so we can
    # show a "no charge — try again" banner instead of looking like
    # the user landed on /pricing for no reason.
    show_canceled_banner = request.args.get("canceled") == "1"

    # If the user landed here mid-flow (e.g. ran out of credits while
    # generating a brief), the redirect that bounced them here passed
    # ?return_to=/path/they/were/on. Stash it in the session so
    # /stripe/success can hop them back after they upgrade. Validated
    # to be a same-origin path so a malicious link can't redirect to
    # an external site.
    return_to = safe_return_url(request.args.get("return_to", ""))
    if return_to:
        session["pricing_return_to"] = return_to

    return render_template(
        "pricing.html",
        plans=list_public_plans(),
        current_plan=current_plan,
        show_canceled_banner=show_canceled_banner,
        # Surface the Stripe-config state so the template can show
        # a "checkout temporarily unavailable" banner and disable
        # plan / topup buttons instead of letting users click into
        # an error flash + redirect.
        stripe_configured=is_stripe_configured(),
    )


@app.route("/subscribe/<plan_slug>", methods=["GET"])
@login_required
def start_subscription(plan_slug):
    """Begin a subscription change. Without Stripe wired up, this is a
    dev-mode plan switch that records a CreditTransaction for audit
    visibility. The Stripe checkout flow plugs in here later."""
    plan = get_plan(plan_slug)
    if plan_slug not in PLAN_CATALOG or plan_slug == "free":
        flash("That plan isn't available.", "error")
        return redirect(url_for("pricing_page"))

    # Stripe placeholder: when STRIPE_SECRET_KEY + price IDs are wired,
    # redirect to a Stripe Checkout session here. For now we set the plan
    # directly and grant the first month's allowance immediately.
    if os.getenv("STRIPE_SECRET_KEY"):
        # TODO: redirect to stripe.checkout.Session.create(...)
        pass

    current_user.plan = plan_slug
    db.session.commit()
    granted = grant_monthly_credits_if_due(current_user)
    if granted:
        flash(
            f"Welcome to {plan['label']} — {granted} credits added to your wallet.",
            "success",
        )
    else:
        flash(f"You're now on the {plan['label']} plan.", "success")
    return redirect(url_for("settings_billing"))


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

    # Pick a "spotlight" workspace whose audit drives the rich
    # dashboard scorecard. Resolution order:
    #   1. ?spotlight=<id> URL param (user override via the
    #      workspace switcher on the dashboard)
    #   2. focused_client (single-mode)
    #   3. the most-recently-audited workspace (multi-mode)
    # This replaces the old branch where multi-mode users got a
    # plain count-based "Portfolio Snapshot" instead of the rich
    # scorecard everyone with one workspace already saw.
    spotlight_param = request.args.get("spotlight", "").strip()
    spotlight_client = None

    if spotlight_param:
        for c in clients:
            if str(c.get("id")) == spotlight_param:
                spotlight_client = c
                break

    if spotlight_client is None and focused_client:
        spotlight_client = focused_client

    if spotlight_client is None and clients:
        spotlight_client = max(
            clients,
            key=lambda c: (c.get("latest_audit") or {}).get("saved_at") or "",
        )

    overall_score = 0
    visibility_score = 0
    content_score = 0
    entity_score = 0
    trust_score = 0

    if spotlight_client:
        latest_audit = spotlight_client.get("latest_audit") or {}

        overall_score = latest_audit.get("normalized_score", 0) or 0
        visibility_score = latest_audit.get("visibility_score", 0) or 0
        content_score = latest_audit.get("content_score", 0) or 0
        entity_score = latest_audit.get("entity_score", 0) or 0
        trust_score = latest_audit.get("trust_score", 0) or 0

    # Account-wide average kept for the portfolio sub-stat in
    # multi-mode (shown alongside the spotlight scorecard, no
    # longer in place of it).
    portfolio_avg_score = 0
    if view_mode == "multi":
        client_scores = [
            (c.get("latest_audit") or {}).get("normalized_score")
            for c in clients
        ]
        client_scores = [s for s in client_scores if s is not None]
        portfolio_avg_score = (
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
        spotlight_client=spotlight_client,
        portfolio_avg_score=portfolio_avg_score,
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


def _resolve_business_profile_for_pdf(workspace_row, client_dict):
    """Lazy-enrich the workspace's business profile if it's missing or
    stale, then return a flat dict the PDF template reads from."""
    from services.business_profile_research import (
        is_profile_stale, research_business_profile,
    )

    if workspace_row and is_profile_stale(workspace_row):
        try:
            data = research_business_profile(
                name=workspace_row.name,
                website=workspace_row.website,
                location=workspace_row.location,
            )
            if data:
                if data.get("founded_year"):
                    workspace_row.founded_year = data["founded_year"]
                if data.get("google_rating"):
                    workspace_row.google_rating = data["google_rating"]
                if data.get("google_review_count") is not None:
                    workspace_row.google_review_count = data["google_review_count"]
                if data.get("executive_summary"):
                    workspace_row.business_summary = data["executive_summary"]
                if data.get("core_services") and not workspace_row.brand_services:
                    workspace_row.brand_services = data["core_services"]
                workspace_row.business_profile_updated_at = datetime.utcnow()
                db.session.commit()
        except Exception as exc:
            logger.warning("Business profile research failed: %s", exc)

    return {
        "founded_year": getattr(workspace_row, "founded_year", None),
        "google_rating": getattr(workspace_row, "google_rating", None),
        "google_review_count": getattr(workspace_row, "google_review_count", None),
        "core_services": (
            getattr(workspace_row, "brand_services", None)
            or client_dict.get("brand_kit", {}).get("services")
        ),
        "executive_summary": getattr(workspace_row, "business_summary", None),
    }


def _strip_competitor_markdown(raw):
    """Sanitise competitor strings the AI engines return. The model
    often answers in markdown bullet form like
        "**Creamier** – Known for artisanal ice cream..."
    We want clean brand names — "Creamier" — for the citation table.
    Strips bold markers, splits on en-dash / colon / parens, and
    truncates the residual descriptor."""
    if not raw:
        return None
    text = str(raw).strip()
    # Strip leading list markers and bold markers.
    text = text.lstrip("-*•· ").rstrip()
    text = text.replace("**", "").replace("__", "")
    # Cut off everything after the first "—", "–", " - ", ":", or "(".
    for sep in ["–", "—", " - ", ":", "("]:
        if sep in text:
            text = text.split(sep, 1)[0].strip()
    # Drop trailing punctuation.
    text = text.rstrip(".,;").strip()
    return text or None


def _citation_table_rows(client_dict, max_rows: int = 5):
    """Build the AI Citation Test Results table.

    Three data sources, in priority order:
      1. PromptTracking — the Answer Monitor's tracked prompts (most
         recent monitor sweep). Best when the user has been actively
         tracking prompts.
      2. Saved audit's `query_analysis` — the queries the audit
         pipeline tested against AI engines. Has real "brand mentioned"
         and "competitors mentioned" data captured during the audit.
      3. Empty list — table doesn't render.

    For new workspaces with one audit and no monitor activity yet,
    source #2 is the difference between an empty report and a
    convincing one.
    """
    domain = (client_dict.get("website_normalized") or "").strip()
    out = []

    # --- Source 1: tracked prompts ----------------------------------
    if domain:
        owner_id = effective_owner_id() or (
            current_user.id if current_user.is_authenticated else None
        )
        if owner_id:
            prompt_rows = (
                PromptTracking.query.filter_by(user_id=owner_id, domain=domain)
                .order_by(PromptTracking.created_at.desc())
                .limit(max_rows)
                .all()
            )
            for row in prompt_rows:
                latest_snap = (
                    db.session.query(PromptCheckSnapshot)
                    .filter_by(prompt_tracking_id=row.id)
                    .order_by(PromptCheckSnapshot.checked_at.desc())
                    .first()
                )
                if latest_snap:
                    cited = bool(latest_snap.brand_mentioned)
                    competitors = latest_snap.competitors_mentioned or []
                    top = competitors[0] if competitors else None
                else:
                    cited = (row.mentioned or "").lower() == "yes"
                    top = row.top_competitor or None
                out.append({
                    "query": row.prompt or "—",
                    "cited": cited,
                    "top_competitor": _strip_competitor_markdown(top) or "—",
                })

    if out:
        return out

    # --- Source 2: saved audit's query_analysis ---------------------
    # Pull from the workspace's most recent audit run. The full audit
    # JSON has the rich per-query results (brand_mentioned + the list
    # of competitor brands the AI engines named).
    latest_audit = client_dict.get("latest_audit") or {}
    rows = latest_audit.get("query_analysis") or []
    if not rows and latest_audit.get("filename"):
        # Summary only loaded — read the full file for query_analysis.
        try:
            full = read_full_audit_data(latest_audit.get("filename"))
            if full:
                rows = full.get("query_analysis") or full.get("ai_answer_results") or []
        except Exception:
            rows = []

    for row in rows[:max_rows]:
        if not isinstance(row, dict):
            continue
        comps = row.get("competitors_mentioned") or row.get("competitors") or []
        top = _strip_competitor_markdown(comps[0]) if comps else None
        out.append({
            "query": row.get("query") or "—",
            "cited": bool(
                row.get("brand_mentioned")
                or row.get("latest_brand_mentioned")
            ),
            "top_competitor": top or "—",
        })
    return out


def _extract_brand_names_from_research(snippets, *, brand_name, count=6):
    """Use OpenAI to pull a clean deduplicated list of competitor
    brand names out of Tavily's research snippets. Tavily returns
    listicle articles ("10 best X in Singapore") whose snippets name
    multiple real brands; we want those names as a list. Falls back
    to [] silently when OpenAI isn't configured or the call fails."""
    if not os.getenv("OPENAI_API_KEY") or not snippets:
        return []
    joined = "\n\n".join(s for s in snippets if s)[:6000]
    if not joined.strip():
        return []
    try:
        from openai import OpenAI
        client = OpenAI()
        prompt = (
            f"From these web search snippets, extract the {count} most "
            f"prominent competing business names (excluding '{brand_name}' "
            f"itself, generic listicle titles, and media outlets). "
            f"Return JSON only: {{\"brands\": [\"…\", …]}}.\n\n"
            f"Snippets:\n{joined}"
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system",
                 "content": "Extract clean brand names from search snippets. Return only real, current brand names — no media outlets, no generic phrases."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=400,
        )
        import json as _json
        parsed = _json.loads(response.choices[0].message.content or "{}")
        brands = parsed.get("brands") or []
        return [str(b).strip() for b in brands if isinstance(b, str) and b.strip()][:count]
    except Exception as exc:
        logger.warning("Brand-name extraction from research failed: %s", exc)
        return []


def _extract_research_pack_for_pdf(client_dict, latest_audit):
    """Pull Tavily research data into PDF-ready shapes.

    Returns (top_competitors, competitor_notes):
      - top_competitors: list of {"name": str, "snippet": str, "url": str}
        for the dedicated Competitors section. Up to 5 brand names
        extracted from the Tavily research snippets via LLM, paired
        with their best-matching source URL.
      - competitor_notes: a short paragraph naming the top 3
        competitors. None when no data.

    Reads from latest_audit.research_pack first, then falls back to
    re-reading the full audit file if the summary doesn't carry the
    research bundle (older saves).
    """
    research = latest_audit.get("research_pack") if latest_audit else None
    if not research and latest_audit and latest_audit.get("filename"):
        try:
            full = read_full_audit_data(latest_audit.get("filename"))
            if full:
                research = full.get("research_pack")
        except Exception:
            research = None

    if not research:
        return [], None

    competitors_raw = (research.get("results") or {}).get("competitors") or []
    if not competitors_raw:
        return [], None

    # Step 1: pull brand names out of the research snippets via LLM.
    snippets = [
        (row.get("snippet") or row.get("content") or "")
        for row in competitors_raw
        if isinstance(row, dict)
    ]
    brand_name = client_dict.get("name") or client_dict.get("website") or "this brand"
    extracted_names = _extract_brand_names_from_research(
        snippets, brand_name=brand_name, count=6
    )

    # Step 2: for each brand, find the most relevant source row.
    top_competitors = []
    seen = set()
    for name in extracted_names:
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        match = None
        for row in competitors_raw:
            if not isinstance(row, dict):
                continue
            haystack = (
                (row.get("title") or "") + " " + (row.get("snippet") or row.get("content") or "")
            ).lower()
            if name.lower() in haystack:
                match = row
                break
        snippet = ""
        url = ""
        if match:
            snippet = (match.get("snippet") or match.get("content") or "")[:200]
            url = match.get("url") or match.get("link") or ""
        top_competitors.append({
            "name": name,
            "url": url,
            "snippet": snippet,
        })
        if len(top_competitors) >= 5:
            break

    if not top_competitors:
        return [], None

    names_top3 = ", ".join(c["name"] for c in top_competitors[:3])
    more = (
        f" + {len(top_competitors) - 3} more"
        if len(top_competitors) > 3 else ""
    )
    competitor_notes = (
        f"Web research surfaced these competitors AI assistants cite "
        f"alongside or ahead of {brand_name}: {names_top3}{more}. The "
        f"Recommended Actions below target the queries where these "
        f"names dominate."
    )

    return top_competitors, competitor_notes


def _build_audit_pdf(workspace_row, client, *, agency_override=None):
    """Render the audit PDF HTML + return (html, filename) so the
    same rendering serves both the authenticated /export-pdf route
    and the public /report/<token> route."""
    business_profile = _resolve_business_profile_for_pdf(workspace_row, client)
    citation_rows = _citation_table_rows(client, max_rows=5)

    latest = client.get("latest_audit", {})
    recommended_actions = client.get("recommended_actions", [])
    question_rows = client.get("question_rows", [])
    missing_rows = client.get("missing_rows", [])
    top_action = recommended_actions[0] if recommended_actions else None

    # Tavily research pack — extract real competitor list and a
    # one-paragraph market summary so the report has substance beyond
    # the AI citation test. Falls back to the saved audit's
    # research_pack key when the live latest dict doesn't have it.
    top_competitors, competitor_notes = _extract_research_pack_for_pdf(
        client, latest
    )

    # Use the override agency dict if provided (public-share path —
    # the workspace owner's branding), otherwise fall back to the
    # current user's effective branding.
    agency_payload = agency_override or effective_agency_branding()
    agency = {
        "name": agency_payload.get("name") or "Your Agency",
        "logo_url": agency_payload.get("logo_url"),
        "website": agency_payload.get("website"),
        "tagline": agency_payload.get("tagline") or "AI Visibility & Content Strategy",
        "footer_text": agency_payload.get("footer_text"),
        "disclaimer": agency_payload.get("disclaimer"),
        "active": agency_payload.get("active", False),
    }

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
        executive_summary=(
            business_profile.get("executive_summary")
            or client.get("executive_summary")
        ),
        competitor_notes=competitor_notes or client.get("competitor_notes"),
        top_competitors=top_competitors,
        business_profile=business_profile,
        citation_rows=citation_rows,
    )
    filename = pdf_filename(f"{client.get('name', 'report')} audit report")
    fallback = client_audit_pdf_lines(client, latest, recommended_actions, report_date)
    return html, filename, fallback


@app.route("/client/<client_id>/export-pdf")
@login_required
def export_client_audit_pdf(client_id):
    client = get_client_by_id(client_id)
    if not client:
        abort(404)

    # The Client ORM row drives the business-profile cache; client (the
    # serialised dict) drives the rest of the rendering.
    workspace_row = (
        Client.query.filter_by(slug=str(client_id), user_id=effective_owner_id())
        .first()
        or (
            Client.query.filter_by(id=int(client_id), user_id=effective_owner_id()).first()
            if str(client_id).isdigit() else None
        )
    )

    html, filename, fallback = _build_audit_pdf(workspace_row, client)
    return render_pdf_response(html, filename, fallback_lines=fallback)


@app.route("/client/<int:client_id>/share/toggle", methods=["POST"])
@login_required
def toggle_public_share(client_id):
    """Generate or revoke a public share link for the workspace's
    audit report. The token lives on the Client row so revoke =
    overwrite. Anyone with the active token can view the report PDF."""
    workspace = (
        Client.query.filter_by(id=client_id, user_id=effective_owner_id())
        .one_or_none()
    )
    if not workspace:
        abort(404)
    action = (request.form.get("action") or "").lower()
    if action == "revoke":
        workspace.public_share_token = None
        workspace.public_share_created_at = None
        flash("Public report link revoked.", "success")
    else:
        workspace.public_share_token = secrets.token_urlsafe(24)
        workspace.public_share_created_at = datetime.utcnow()
        flash("Public report link is live. Copy it to share.", "success")
    db.session.commit()
    return redirect(url_for("client_detail", client_id=client_id))


@app.route("/report/<token>")
def public_audit_report(token):
    """Public-facing audit report — no auth required. Looks up the
    workspace by token and renders the report HTML inline (download
    via /report/<token>/pdf). Reuses the owner's white-label branding
    so the report carries the agency's identity end-to-end."""
    workspace = Client.query.filter_by(public_share_token=token).one_or_none()
    if not workspace:
        abort(404)

    owner = User.query.get(workspace.user_id)
    agency_payload = agency_branding(owner) if owner else agency_branding(None)
    client = serialize_client_row(workspace)

    # Build the dict shape the audit PDF template expects. We pull
    # the latest saved audit for this workspace if there is one.
    audits = get_saved_audits(user_id=workspace.user_id)
    matched = [
        a for a in audits
        if (a.get("client_id") and str(a.get("client_id")) == str(workspace.id))
        or a.get("website_normalized") == workspace.website_normalized
    ]
    matched = sort_audits(matched, sort_by="saved_at", order="desc") if matched else []
    latest_audit = matched[0] if matched else {}
    client["latest_audit"] = latest_audit
    client["recommended_actions"] = []
    client["question_rows"] = (latest_audit or {}).get("query_analysis", [])
    client["missing_rows"] = []

    business_profile = _resolve_business_profile_for_pdf(workspace, client)
    # Citation rows for the public path: read directly from the
    # workspace's tracked prompts since current_user isn't authenticated.
    citation_rows = []
    if workspace.website_normalized:
        prompt_rows = (
            PromptTracking.query.filter_by(
                user_id=workspace.user_id, domain=workspace.website_normalized
            )
            .order_by(PromptTracking.created_at.desc())
            .limit(5)
            .all()
        )
        for row in prompt_rows:
            latest_snap = (
                db.session.query(PromptCheckSnapshot)
                .filter_by(prompt_tracking_id=row.id)
                .order_by(PromptCheckSnapshot.checked_at.desc())
                .first()
            )
            if latest_snap:
                cited = bool(latest_snap.brand_mentioned)
                top = (latest_snap.competitors_mentioned or [None])[0] if latest_snap.competitors_mentioned else None
            else:
                cited = (row.mentioned or "").lower() == "yes"
                top = row.top_competitor or None
            citation_rows.append({
                "query": row.prompt or "—",
                "cited": cited,
                "top_competitor": top or "—",
            })

    return render_template(
        "public_audit_report.html",
        client=client,
        latest=client["latest_audit"],
        recommended_actions=[],
        top_action=None,
        question_rows=client["question_rows"],
        missing_rows=[],
        report_date=datetime.utcnow().strftime("%d %b %Y"),
        agency={
            "name": agency_payload.get("name") or "Your Agency",
            "logo_url": agency_payload.get("logo_url"),
            "website": agency_payload.get("website"),
            "tagline": agency_payload.get("tagline") or "AI Visibility & Content Strategy",
            "footer_text": agency_payload.get("footer_text"),
            "disclaimer": agency_payload.get("disclaimer"),
            "active": agency_payload.get("active", False),
        },
        executive_summary=(
            business_profile.get("executive_summary")
            or client.get("executive_summary")
        ),
        competitor_notes=client.get("competitor_notes"),
        business_profile=business_profile,
        citation_rows=citation_rows,
        token=token,
    )


@app.route("/report/<token>/pdf")
def public_audit_report_pdf(token):
    """Same data as the public HTML view, rendered as a PDF."""
    workspace = Client.query.filter_by(public_share_token=token).one_or_none()
    if not workspace:
        abort(404)

    # Reuse the public HTML route's data path then render via WeasyPrint.
    # Easiest way: hit the route internally so we don't duplicate the
    # data assembly. Then strip the surrounding "share frame" wrapper.
    with app.test_request_context(f"/report/{token}"):
        # re-fetch through the same code path
        owner = User.query.get(workspace.user_id)
        agency_payload = agency_branding(owner) if owner else agency_branding(None)
        client = serialize_client_row(workspace)
        audits = get_saved_audits(user_id=workspace.user_id)
        matched = [
            a for a in audits
            if (a.get("client_id") and str(a.get("client_id")) == str(workspace.id))
            or a.get("website_normalized") == workspace.website_normalized
        ]
        matched = sort_audits(matched, sort_by="saved_at", order="desc") if matched else []
        client["latest_audit"] = matched[0] if matched else {}
        client["recommended_actions"] = []
        client["question_rows"] = (matched[0] or {}).get("query_analysis", []) if matched else []
        client["missing_rows"] = []

    html, filename, fallback = _build_audit_pdf(
        workspace, client, agency_override=agency_payload
    )
    return render_pdf_response(html, filename, fallback_lines=fallback)


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
    """Total workspaces this user can create: plan base limit + extra
    workspaces purchased as add-ons. Admins / dev_unlimited get no cap."""
    if not user:
        return 0
    if user.role == "admin" or user.plan == "dev_unlimited":
        return None
    base = workspace_limit_for_plan(getattr(user, "plan", "free") or "free")
    extras = int(getattr(user, "extra_workspaces", 0) or 0)
    return base + extras


def get_workspace_count(user_id):
    """Count workspaces against the OWNING account so team members
    can't accidentally bypass the cap."""
    user = User.query.get(user_id) if user_id else None
    target = effective_owner(user) if user else None
    target_id = target.id if target else user_id
    return Client.query.filter_by(user_id=target_id).count()


def can_create_workspace(user):
    limit = get_workspace_limit(user)
    count = get_workspace_count(user.id)

    if limit is None:
        return True, None, count

    return count < limit, limit, count


# =========================
# Stripe checkout (bundles + subscriptions)
# =========================
# Three routes:
#   /stripe/checkout/bundle/<credits>  — one-time topup
#   /stripe/checkout/plan/<plan_slug>  — recurring subscription
#   /stripe/webhook                    — credits land + plans flip here
# Plus /stripe/portal so the user can self-serve cancel / payment method.


@app.route("/stripe/checkout/bundle/<int:credits>", methods=["GET", "POST"])
@login_required
def stripe_checkout_bundle(credits):
    from services.stripe_helper import (
        StripeNotConfigured,
        create_bundle_checkout_session,
        is_stripe_configured,
    )

    if not is_stripe_configured():
        flash(
            "Stripe isn't configured on this server yet. Reach out to support to top up credits.",
            "error",
        )
        return redirect(url_for("settings_credits"))

    try:
        # Pass the purchase shape via URL params so /stripe/success can
        # render the appropriate "you just bought 25 credits" framing
        # without hitting the Stripe API to fetch session metadata.
        success_url = (
            url_for("stripe_success", _external=True)
            + f"?kind=bundle&credits={int(credits)}"
        )
        cancel_url = (
            url_for("settings_credits", _external=True) + "?canceled=1"
        )
        result = create_bundle_checkout_session(
            user_id=current_user.id,
            user_email=current_user.email,
            credits=credits,
            is_subscriber=is_subscriber(getattr(current_user, "plan", "free")),
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except StripeNotConfigured as exc:
        flash(f"Topup unavailable: {exc}", "error")
        return redirect(url_for("settings_credits"))
    except Exception as exc:
        logger.warning("Stripe bundle checkout failed: %s", exc)
        flash("Could not start checkout. Try again in a moment.", "error")
        return redirect(url_for("settings_credits"))

    return redirect(result["url"], code=303)


@app.route("/stripe/checkout/plan/<plan_slug>", methods=["GET", "POST"])
@login_required
def stripe_checkout_plan(plan_slug):
    from services.stripe_helper import (
        StripeNotConfigured,
        create_subscription_checkout_session,
        is_stripe_configured,
    )

    if plan_slug not in PLAN_CATALOG or plan_slug == "free":
        flash("That plan isn't available.", "error")
        return redirect(url_for("pricing_page"))

    if not is_stripe_configured():
        # Dev fallback: flip the plan immediately so the demo flow works
        # without Stripe wired up. Production deployments should always
        # have STRIPE_SECRET_KEY set so this branch never fires.
        return redirect(url_for("start_subscription", plan_slug=plan_slug))

    try:
        success_url = (
            url_for("stripe_success", _external=True)
            + f"?kind=plan&slug={plan_slug}"
        )
        # Cancel returns to /pricing with a flag so we can show a
        # gentle "no charge — try again or pick a different plan"
        # banner instead of looking like nothing happened.
        cancel_url = (
            url_for("pricing_page", _external=True) + "?canceled=1"
        )
        result = create_subscription_checkout_session(
            user_id=current_user.id,
            user_email=current_user.email,
            plan_slug=plan_slug,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except StripeNotConfigured as exc:
        flash(f"Plan checkout unavailable: {exc}", "error")
        return redirect(url_for("pricing_page"))
    except Exception as exc:
        logger.warning("Stripe subscription checkout failed: %s", exc)
        flash("Could not start checkout. Try again in a moment.", "error")
        return redirect(url_for("pricing_page"))

    return redirect(result["url"], code=303)


@app.route("/stripe/success")
@login_required
def stripe_success():
    """Landing page after Stripe Checkout completes.

    The wallet / plan update happens server-side in the webhook, so
    this page just renders confirmation + a "what to do next" CTA.
    Purchase context is passed via URL params (?kind=plan&slug=pro
    or ?kind=bundle&credits=25) so the framing is specific to what
    the user actually bought, not a one-size-fits-all flash."""
    kind = request.args.get("kind", "").strip()
    plan_slug = request.args.get("slug", "").strip().lower()
    credits_param = request.args.get("credits", "").strip()

    purchase = {"kind": "unknown"}

    if kind == "plan" and plan_slug in PLAN_CATALOG:
        plan = PLAN_CATALOG.get(plan_slug, {})
        purchase = {
            "kind": "plan",
            "slug": plan_slug,
            "label": plan.get("label", plan_slug.title()),
            "tagline": plan.get("tagline", ""),
            "features": plan.get("features", []) or [],
        }
    elif kind == "bundle" and credits_param.isdigit():
        purchase = {
            "kind": "bundle",
            "credits": int(credits_param),
        }

    # First workspace exists? If so, link the next-step CTA to its
    # audit form; otherwise to /clients/new.
    workspaces = build_client_views()
    next_workspace_id = workspaces[0]["id"] if workspaces else None

    # Deep-link recovery: if the user was bounced to /pricing while
    # in the middle of a flow (audit / brief / draft credit-out),
    # /pricing stashed the originating path in session under
    # `pricing_return_to`. Surface it to the success template so the
    # primary CTA can offer "Back to where you were" as a one-click
    # return. Pop it so a future success page doesn't reuse a stale
    # one. Validated on the way in via safe_return_url.
    return_to = session.pop("pricing_return_to", None)
    return_to = safe_return_url(return_to) if return_to else None

    return render_template(
        "stripe_success.html",
        purchase=purchase,
        next_workspace_id=next_workspace_id,
        has_any_workspace=bool(workspaces),
        return_to=return_to,
    )


@app.route("/billing/buy-workspace", methods=["POST"])
@login_required
def buy_extra_workspace():
    """Purchase one additional workspace beyond the plan's base cap.

    Free plan can't buy add-ons. Stripe-backed when STRIPE_SECRET_KEY +
    STRIPE_PRICE_EXTRA_WORKSPACE are set; otherwise this is a dev-mode
    increment so we can test the flow before billing is wired."""
    if not plan_allows_workspace_addon(current_user.plan):
        flash(
            "Extra workspaces are only available on paid plans. "
            "Upgrade your plan to add more workspaces.",
            "warning",
        )
        return redirect(url_for("pricing_page"))

    stripe_price = os.getenv("STRIPE_PRICE_EXTRA_WORKSPACE")
    if os.getenv("STRIPE_SECRET_KEY") and stripe_price:
        # Real Stripe path — create a one-off Checkout session that
        # adds the recurring addon as a subscription item via metadata
        # the webhook will read.
        try:
            import stripe as _stripe
            _stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
            session = _stripe.checkout.Session.create(
                mode="subscription",
                line_items=[{"price": stripe_price, "quantity": 1}],
                client_reference_id=str(current_user.id),
                customer=current_user.stripe_customer_id or None,
                customer_email=current_user.email if not current_user.stripe_customer_id else None,
                metadata={
                    "kind": "extra_workspace",
                    "user_id": str(current_user.id),
                },
                success_url=url_for("settings_billing", _external=True),
                cancel_url=url_for("settings_billing", _external=True),
            )
            return redirect(session.url, code=303)
        except Exception as exc:
            logger.warning("Extra workspace checkout failed: %s", exc)
            flash("Could not start checkout. Try again in a moment.", "error")
            return redirect(url_for("settings_billing"))

    # Dev fallback — increment the count directly so the workspace flow
    # can be tested before Stripe is configured.
    current_user.extra_workspaces = int(current_user.extra_workspaces or 0) + 1
    db.session.commit()
    flash(
        f"Added 1 extra workspace (you now have "
        f"{get_workspace_limit(current_user)} total). "
        "Note: Stripe billing isn't wired yet — this was a dev-mode increment.",
        "success",
    )
    return redirect(url_for("settings_billing"))


@app.route("/billing/buy-seat", methods=["POST"])
@login_required
def buy_extra_seat():
    """Purchase one additional team seat ($5/mo recurring)."""
    from pricing import plan_allows_seat_addon

    if not plan_allows_seat_addon(current_user.plan):
        flash(
            "Extra team seats are only available on Pro and Growth plans.",
            "warning",
        )
        return redirect(url_for("pricing_page"))

    stripe_price = os.getenv("STRIPE_PRICE_EXTRA_SEAT")
    if os.getenv("STRIPE_SECRET_KEY") and stripe_price:
        try:
            import stripe as _stripe
            _stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
            session = _stripe.checkout.Session.create(
                mode="subscription",
                line_items=[{"price": stripe_price, "quantity": 1}],
                client_reference_id=str(current_user.id),
                customer=current_user.stripe_customer_id or None,
                customer_email=current_user.email if not current_user.stripe_customer_id else None,
                metadata={"kind": "extra_seat", "user_id": str(current_user.id)},
                success_url=url_for("settings_team", _external=True),
                cancel_url=url_for("settings_team", _external=True),
            )
            return redirect(session.url, code=303)
        except Exception as exc:
            logger.warning("Extra seat checkout failed: %s", exc)
            flash("Could not start checkout. Try again in a moment.", "error")
            return redirect(url_for("settings_team"))

    current_user.extra_seats = int(current_user.extra_seats or 0) + 1
    db.session.commit()
    flash(
        f"Added 1 extra seat (you now have {get_seat_limit(current_user)} total). "
        "Note: Stripe billing isn't wired yet — this was a dev-mode increment.",
        "success",
    )
    return redirect(url_for("settings_team"))


@app.route("/billing/release-seat", methods=["POST"])
@login_required
def release_extra_seat():
    """Release one paid extra seat. Refuses if it would orphan a member
    (current member count > new total)."""
    if int(current_user.extra_seats or 0) <= 0:
        flash("No paid extra seats to release.", "info")
        return redirect(url_for("settings_team"))

    used = count_team_members(current_user.id)
    new_total = get_seat_limit(current_user) - 1
    if used > new_total:
        flash(
            f"You're using {used} seats — revoke a member or pending invite first.",
            "warning",
        )
        return redirect(url_for("settings_team"))

    current_user.extra_seats = int(current_user.extra_seats or 0) - 1
    db.session.commit()
    flash("Released 1 extra seat.", "success")
    return redirect(url_for("settings_team"))


@app.route("/team/invite", methods=["POST"])
@login_required
def team_invite():
    """Create a pending TeamInvite + return the share URL.

    No email service yet — the owner copies the URL to send manually.
    Pending invites count toward the seat cap so the owner can't
    oversubscribe."""
    from pricing import plan_allows_seat_addon
    if not plan_allows_seat_addon(current_user.plan):
        flash(
            "Team invites are only available on Pro and Growth plans.",
            "warning",
        )
        return redirect(url_for("settings_team"))

    if current_user.team_owner_id:
        flash("Only the team owner can invite members.", "error")
        return redirect(url_for("settings_team"))

    email = (request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        flash("Please enter a valid email address.", "error")
        return redirect(url_for("settings_team"))

    used = count_team_members(current_user.id)
    if used >= get_seat_limit(current_user):
        flash(
            f"You've used all {get_seat_limit(current_user)} seats. "
            "Buy an extra seat or revoke an existing invite first.",
            "warning",
        )
        return redirect(url_for("settings_team"))

    # Soft-dedupe: don't create a second pending invite for the same email.
    existing = TeamInvite.query.filter_by(
        owner_user_id=current_user.id, email=email, status="pending"
    ).first()
    if existing:
        flash(f"An invite is already pending for {email}.", "info")
        return redirect(url_for("settings_team"))

    invite = TeamInvite(
        owner_user_id=current_user.id,
        email=email,
        token=secrets.token_urlsafe(24),
        status="pending",
    )
    db.session.add(invite)
    db.session.commit()
    invite_url = url_for("team_accept", token=invite.token, _external=True)

    # Try to send the invite email. Falls back to flashing the URL when
    # SMTP isn't configured so dev environments still work.
    from services.email_helper import (
        is_email_configured, render_team_invite_email, send_email,
    )
    delivered = False
    if is_email_configured():
        subject, text, html = render_team_invite_email(
            owner_name=current_user.name or current_user.email,
            invitee_email=email,
            invite_url=invite_url,
        )
        delivered = send_email(
            to=email,
            subject=subject,
            body_text=text,
            body_html=html,
            reply_to=current_user.email,
        )

    if delivered:
        flash(
            f"Invite emailed to {email}. They'll get a link that expires when accepted.",
            "success",
        )
    else:
        flash(
            f"Invite ready for {email}. Copy this link and send it to them: {invite_url}",
            "success",
        )
    return redirect(url_for("settings_team"))


@app.route("/team/accept/<token>", methods=["GET", "POST"])
def team_accept(token):
    """Accept a pending invite. If the recipient already has an account
    matching the invite email and is logged in, attach immediately;
    otherwise show a tiny acceptance form that creates a new account."""
    invite = TeamInvite.query.filter_by(token=token, status="pending").first()
    if not invite:
        flash("This invite is invalid or has already been used.", "error")
        return redirect(url_for("login"))

    owner = User.query.get(invite.owner_user_id)
    if not owner:
        flash("This invite's owner account could not be found.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        # Acceptance: either logged-in user (must match email) or new signup.
        if current_user.is_authenticated:
            if current_user.email.lower() != invite.email.lower():
                flash(
                    f"This invite is for {invite.email}. Sign out first to accept it as that email.",
                    "error",
                )
                return redirect(url_for("team_accept", token=token))
            if current_user.team_owner_id and current_user.team_owner_id != owner.id:
                flash("You're already a member of another team.", "error")
                return redirect(url_for("index"))
            current_user.team_owner_id = owner.id
            invite.status = "accepted"
            invite.accepted_at = datetime.utcnow()
            invite.accepted_user_id = current_user.id
            db.session.commit()
            flash(f"Joined {owner.name}'s team.", "success")
            return redirect(url_for("index"))

        # New-signup path: create User row + log in + attach.
        name = (request.form.get("name") or "").strip()
        password = request.form.get("password") or ""
        if not name or len(password) < 8:
            flash("Name and 8+ char password required.", "error")
            return redirect(url_for("team_accept", token=token))
        existing = User.query.filter_by(email=invite.email).first()
        if existing:
            flash("An account with that email already exists. Log in first to accept.", "error")
            return redirect(url_for("login"))

        new_user = User(
            email=invite.email,
            password_hash=generate_password_hash(password),
            name=name,
            referral_code=secrets.token_urlsafe(6),
            plan="free",
            team_owner_id=owner.id,
        )
        db.session.add(new_user)
        db.session.flush()
        # Ensure they have a (zero-balance) wallet so wallet checks don't crash.
        db.session.add(Wallet(user_id=new_user.id, balance=0))
        invite.status = "accepted"
        invite.accepted_at = datetime.utcnow()
        invite.accepted_user_id = new_user.id
        db.session.commit()
        login_user(new_user)
        flash(f"Account created. You're now part of {owner.name}'s team.", "success")
        return redirect(url_for("index"))

    return render_template(
        "team_accept.html",
        invite=invite,
        owner=owner,
        already_logged_in=current_user.is_authenticated,
    )


@app.route("/team/revoke/<int:invite_id>", methods=["POST"])
@login_required
def team_revoke_invite(invite_id):
    """Revoke a pending invite (only the owner can do this)."""
    invite = TeamInvite.query.filter_by(
        id=invite_id, owner_user_id=current_user.id
    ).one_or_none()
    if not invite:
        flash("Invite not found.", "error")
        return redirect(url_for("settings_team"))
    if invite.status != "pending":
        flash("Only pending invites can be revoked.", "info")
        return redirect(url_for("settings_team"))
    invite.status = "revoked"
    db.session.commit()
    flash(f"Revoked invite for {invite.email}.", "success")
    return redirect(url_for("settings_team"))


@app.route("/team/remove-member/<int:member_id>", methods=["POST"])
@login_required
def team_remove_member(member_id):
    """Remove an active team member (frees the seat)."""
    member = User.query.filter_by(
        id=member_id, team_owner_id=current_user.id
    ).one_or_none()
    if not member:
        flash("Member not found on your team.", "error")
        return redirect(url_for("settings_team"))
    member.team_owner_id = None
    db.session.commit()
    flash(f"Removed {member.email} from your team.", "success")
    return redirect(url_for("settings_team"))


@app.route("/billing/release-workspace", methods=["POST"])
@login_required
def release_extra_workspace():
    """Release one paid extra-workspace slot (refund/cancel-style).

    Refuses if the user is currently using more workspaces than the
    base cap minus 1 (i.e. would orphan a workspace)."""
    if int(current_user.extra_workspaces or 0) <= 0:
        flash("No paid extra workspaces to release.", "info")
        return redirect(url_for("settings_billing"))

    base = workspace_limit_for_plan(current_user.plan)
    used = get_workspace_count(current_user.id)
    new_total = base + int(current_user.extra_workspaces) - 1
    if used > new_total:
        flash(
            f"You're using {used} workspaces — delete one before releasing an extra slot.",
            "warning",
        )
        return redirect(url_for("settings_billing"))

    current_user.extra_workspaces = int(current_user.extra_workspaces or 0) - 1
    db.session.commit()
    flash(
        "Released 1 extra workspace. (Stripe billing isn't wired yet — "
        "in production this would prorate the addon on your next invoice.)",
        "success",
    )
    return redirect(url_for("settings_billing"))


@app.route("/stripe/portal", methods=["GET", "POST"])
@login_required
def stripe_portal():
    from services.stripe_helper import (
        StripeNotConfigured,
        create_billing_portal_session,
        is_stripe_configured,
    )

    if not is_stripe_configured() or not getattr(current_user, "stripe_customer_id", None):
        flash(
            "Open the billing portal after your first Stripe checkout. "
            "If you've already paid, contact support.",
            "info",
        )
        return redirect(url_for("settings_billing"))

    try:
        result = create_billing_portal_session(
            customer_id=current_user.stripe_customer_id,
            return_url=url_for("settings_billing", _external=True),
        )
    except StripeNotConfigured as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings_billing"))

    return redirect(result["url"], code=303)


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Receive checkout.session.completed events and credit the wallet
    or flip the plan accordingly. Always returns 200 once parsed so
    Stripe doesn't retry on application errors."""
    from services.stripe_helper import StripeNotConfigured, construct_webhook_event

    payload = request.get_data()
    signature = request.headers.get("Stripe-Signature", "")
    try:
        event = construct_webhook_event(payload, signature)
    except StripeNotConfigured as exc:
        logger.warning("Stripe webhook hit but not configured: %s", exc)
        return jsonify({"ok": False, "error": "not_configured"}), 503
    except Exception as exc:
        logger.warning("Stripe webhook signature check failed: %s", exc)
        return jsonify({"ok": False, "error": "invalid_signature"}), 400

    event_type = event.get("type") or ""
    data = (event.get("data") or {}).get("object") or {}

    if event_type == "checkout.session.completed":
        try:
            metadata = data.get("metadata") or {}
            kind = metadata.get("kind")
            user_id = int(metadata.get("user_id") or 0)
            user = db.session.get(User, user_id) if user_id else None
            if not user:
                return jsonify({"ok": True, "ignored": "user_not_found"})

            customer_id = data.get("customer")
            if customer_id and not user.stripe_customer_id:
                user.stripe_customer_id = customer_id

            if kind == "bundle":
                credits = int(metadata.get("credits") or 0)
                if credits > 0:
                    if not user.wallet:
                        user.wallet = Wallet(user_id=user.id, balance=0)
                        db.session.add(user.wallet)
                        db.session.flush()
                    user.wallet.balance += credits
                    db.session.add(
                        CreditTransaction(
                            user_id=user.id,
                            type="topup_bundle",
                            amount=credits,
                            balance_after=user.wallet.balance,
                            notes=f"Stripe topup: {credits} credits",
                        )
                    )
            elif kind == "subscription":
                plan_slug = metadata.get("plan_slug")
                if plan_slug in PLAN_CATALOG and plan_slug != "free":
                    user.plan = plan_slug
                    user.stripe_subscription_id = data.get("subscription")
                    grant_monthly_credits_if_due(user)
            elif kind == "extra_workspace":
                user.extra_workspaces = int(user.extra_workspaces or 0) + 1
            elif kind == "extra_seat":
                user.extra_seats = int(user.extra_seats or 0) + 1
            elif kind == "modules":
                # Multi-line-item subscription. Pull the live subscription
                # from Stripe to get authoritative line-item IDs (the
                # checkout.session payload doesn't include them).
                _sync_user_modules_from_subscription(
                    user=user,
                    subscription_id=data.get("subscription"),
                )
            db.session.commit()

            # Referral payout — fires on EVERY paid purchase within the
            # 30-day signup window. amount_total is in cents per Stripe.
            try:
                amount_total = data.get("amount_total")
                if amount_total is not None and kind in (
                    "bundle", "subscription", "extra_workspace", "extra_seat"
                ):
                    award_referral_for_payment(
                        referred_user=user,
                        amount_usd=float(amount_total) / 100.0,
                        stripe_event_ref=data.get("id") or "",
                    )
            except Exception as exc:
                logger.warning("Referral payout failed: %s", exc)
        except Exception as exc:
            logger.error("Stripe webhook handling failed: %s", exc)
            db.session.rollback()

    elif event_type == "customer.subscription.updated":
        # Module subscriptions: a line item added or removed via the
        # billing portal flips UserModule rows accordingly.
        try:
            sub_id = data.get("id")
            if sub_id:
                _sync_user_modules_for_subscription_id(sub_id, subscription_obj=data)
                db.session.commit()
        except Exception as exc:
            logger.error("Stripe subscription update handling failed: %s", exc)
            db.session.rollback()

    elif event_type == "customer.subscription.deleted":
        try:
            sub_id = data.get("id")
            if sub_id:
                # Legacy plan subscription path.
                user = User.query.filter_by(stripe_subscription_id=sub_id).first()
                if user:
                    user.plan = "free"
                    user.stripe_subscription_id = None
                # Modules path: cancel every active module row tied to
                # this subscription regardless of plan/customer linkage.
                module_rows = (
                    UserModule.query
                    .filter_by(stripe_subscription_id=sub_id, status="active")
                    .all()
                )
                for row in module_rows:
                    row.status = "canceled"
                    row.deactivated_at = datetime.utcnow()
                db.session.commit()
        except Exception as exc:
            logger.error("Stripe subscription delete handling failed: %s", exc)
            db.session.rollback()

    return jsonify({"ok": True})


def _sync_user_modules_from_subscription(*, user, subscription_id: Optional[str]) -> None:
    """Fetch a Stripe subscription and write UserModule rows for each
    line item. Idempotent: re-running upserts by subscription_item_id."""
    if not subscription_id or not user:
        return
    from services.stripe_helper import _stripe_module, StripeNotConfigured
    try:
        stripe = _stripe_module()
    except StripeNotConfigured:
        return
    sub = stripe.Subscription.retrieve(subscription_id)
    _apply_subscription_to_user_modules(user_id=user.id, subscription=sub)


def _sync_user_modules_for_subscription_id(
    sub_id: str, *, subscription_obj: Optional[Dict[str, Any]] = None
) -> None:
    """For subscription.updated webhooks. Resolves the user via existing
    UserModule rows tied to this subscription_id (or the user table's
    stripe_subscription_id if this was a legacy-plan sub)."""
    if not sub_id:
        return
    existing = (
        UserModule.query
        .filter_by(stripe_subscription_id=sub_id)
        .first()
    )
    user_id: Optional[int] = existing.user_id if existing else None
    if user_id is None:
        # No module rows yet — could be a legacy plan sub being updated;
        # nothing module-side to do.
        return
    sub = subscription_obj
    if sub is None:
        from services.stripe_helper import _stripe_module, StripeNotConfigured
        try:
            stripe = _stripe_module()
        except StripeNotConfigured:
            return
        sub = stripe.Subscription.retrieve(sub_id)
    _apply_subscription_to_user_modules(user_id=user_id, subscription=sub)


def _apply_subscription_to_user_modules(*, user_id: int, subscription: Any) -> None:
    """Reconcile UserModule rows against a Stripe subscription's line
    items. Adds rows for new items, marks rows canceled for items that
    have disappeared, leaves matching rows alone."""
    from modules import MODULE_CATALOG
    sub_id = subscription.get("id") if isinstance(subscription, dict) else subscription.id
    items_raw = (subscription.get("items") if isinstance(subscription, dict) else subscription.items) or {}
    items_data = items_raw.get("data") if isinstance(items_raw, dict) else getattr(items_raw, "data", []) or []
    period_end_ts = (
        subscription.get("current_period_end")
        if isinstance(subscription, dict)
        else getattr(subscription, "current_period_end", None)
    )
    period_end = (
        datetime.utcfromtimestamp(int(period_end_ts)) if period_end_ts else None
    )

    # Build a price_id → module_slug map from the catalog so we can
    # recognise which line items represent modules vs other line items
    # the customer might have on the same subscription.
    price_to_slug: Dict[str, str] = {}
    for slug, mod in MODULE_CATALOG.items():
        env_var = mod.get("stripe_price_env")
        if env_var:
            price_id = os.getenv(env_var) or ""
            if price_id:
                price_to_slug[price_id] = slug

    # Walk live items: upsert one UserModule row per module item.
    seen_item_ids: set[str] = set()
    for item in items_data:
        item_id = item.get("id") if isinstance(item, dict) else item.id
        price_obj = item.get("price") if isinstance(item, dict) else item.price
        price_id = (
            price_obj.get("id") if isinstance(price_obj, dict) else getattr(price_obj, "id", None)
        )
        slug = price_to_slug.get(price_id or "")
        if not slug:
            continue
        seen_item_ids.add(item_id)
        row = (
            UserModule.query
            .filter_by(stripe_subscription_item_id=item_id)
            .first()
        )
        if row is None:
            row = UserModule(
                user_id=user_id,
                module_slug=slug,
                stripe_subscription_id=sub_id,
                stripe_subscription_item_id=item_id,
                status="active",
                current_period_end=period_end,
                activated_at=datetime.utcnow(),
            )
            db.session.add(row)
        else:
            row.status = "active"
            row.current_period_end = period_end
            row.deactivated_at = None

    # Cancel rows whose items have disappeared from this subscription.
    stale = (
        UserModule.query
        .filter_by(stripe_subscription_id=sub_id, status="active")
        .all()
    )
    for row in stale:
        if row.stripe_subscription_item_id and row.stripe_subscription_item_id not in seen_item_ids:
            row.status = "canceled"
            row.deactivated_at = datetime.utcnow()


@app.route("/clients/new", methods=["GET", "POST"])
@login_required
def create_client():
    view_mode = get_view_mode(current_user)
    existing_clients = build_client_views()
    is_first_workspace = len(existing_clients) == 0

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
                is_first_workspace=is_first_workspace,
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

        # First-workspace users skip the workspace overview (which is
        # mostly empty placeholders pre-audit) and go straight to the
        # audit form — that's the next step on the onboarding stepper
        # and the only non-trivial thing they can do now.
        if is_first_workspace:
            flash(
                "Workspace created. Now run your first audit — uses 1 of your starter credits.",
                "success",
            )
            return redirect(
                url_for("new_audit", client_id=client["id"])
            )

        flash("Client workspace created successfully.", "success")
        return redirect(url_for("client_detail", client_id=client["id"]))

    return render_template(
        "client_form.html",
        error=None,
        form_data={},
        mode="create",
        client=None,
        is_first_workspace=is_first_workspace,
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


@app.route("/client/<client_id>/brand-context/suggest", methods=["POST"])
@login_required
def client_brand_context_suggest(client_id):
    """Draft each brand-context field using gpt-4o-mini.

    Cost is tiny (~$0.0005 per call at ~1500 input + 600 output tokens
    on gpt-4o-mini). The endpoint never overwrites — the JS only fills
    fields the user hasn't touched. Returns JSON so the form can
    populate textareas client-side without a page reload."""
    row = Client.query.filter_by(
        slug=str(client_id), user_id=current_user.id
    ).first()
    if not row and str(client_id).isdigit():
        row = Client.query.filter_by(
            id=int(client_id), user_id=current_user.id
        ).first()
    if not row:
        return {"error": "Workspace not found."}, 404

    # Pull every signal we already have on the workspace so the model
    # can ground the suggestions in real context, not generic SaaS copy.
    parts = [
        f"Brand name: {row.name or 'Unknown'}",
        f"Website: {row.website or 'Not provided'}",
    ]
    if getattr(row, "industry", None) and row.industry != "N/A":
        parts.append(f"Industry: {row.industry}")
    if getattr(row, "location", None) and row.location != "N/A":
        parts.append(f"Location: {row.location}")
    if getattr(row, "owner_type", None):
        parts.append(f"Type: {row.owner_type}")
    if getattr(row, "business_summary", None):
        parts.append(f"Existing summary:\n{row.business_summary}")
    if row.notes:
        parts.append(f"Existing notes:\n{row.notes}")

    context = "\n\n".join(parts)

    system = (
        "You draft brand-context answers for a marketing-AI tool. "
        "Given a workspace's name, website, industry, and location, "
        "you output a JSON object with eight short, concrete fields "
        "the team can edit. Be specific, not generic — name real "
        "audience segments, real services, real differentiators when "
        "evidence supports it. If a field can't be inferred, give a "
        "plausible draft phrased as a starting point, not a fact."
    )

    user_prompt = (
        f"{context}\n\n"
        "Return a JSON object with exactly these keys: "
        '"audience", "services", "differentiators", "proof", "tone", '
        '"locations", "avoid", "extra_notes". Each value should be a '
        "single concise paragraph (1-3 sentences) suitable for a "
        "form textarea. Use the brand name explicitly when it makes "
        "the answer feel grounded. Match the tone field to the brand: "
        "casual brands get casual tone, B2B gets professional, etc."
    )

    try:
        from openai import OpenAI
        client = OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.6,
            max_tokens=900,
        )
        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("brand-context AI suggest failed: %s", exc)
        return {"error": f"AI draft failed: {exc}"}, 502

    # Whitelist the keys we expose so model hallucinations can't pollute
    # the form with random extras.
    allowed = ("audience", "services", "differentiators", "proof",
               "tone", "locations", "avoid", "extra_notes")
    return {k: (data.get(k) or "").strip() for k in allowed}


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
    # Pull the ORM row alongside the serialized dict so the template
    # can read columns the dict doesn't expose (e.g. public_share_token).
    workspace_row = (
        Client.query.filter_by(slug=str(client_id), user_id=effective_owner_id())
        .first()
        or (
            Client.query.filter_by(id=int(client_id), user_id=effective_owner_id()).first()
            if str(client_id).isdigit() else None
        )
    )
    return render_template(
        "client_detail.html",
        client=client,
        workspace_row=workspace_row,
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
            # Preserve name + email so user only retypes the missing
            # field, never echo the password.
            return render_template(
                "signup.html",
                error="All fields are required.",
                form_name=name,
                form_email=email,
                form_referral_code=referral_code,
            )

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            return render_template(
                "signup.html",
                error="Email already registered.",
                form_name=name,
                form_email=email,
                form_referral_code=referral_code,
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
        flash(
            "Welcome! You have 3 starter credits — enough for your first audit. "
            "Set up your workspace to begin.",
            "success",
        )
        return redirect(url_for("create_client"))

    return render_template(
        "signup.html",
        error=None,
        form_name="",
        form_email="",
        form_referral_code=request.args.get("ref", "").strip(),
    )


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Request a password-reset link. Always returns the same flash
    message regardless of whether the email exists — prevents
    enumerating accounts. The actual email send is gated on SMTP
    being configured; without it the flash includes a copy-able URL
    so dev environments still work."""
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email).first() if email else None

        # The reveal-nothing response — same regardless of match.
        generic_flash = (
            "If an account exists for that email, a reset link is on its way."
        )

        if user:
            token = secrets.token_urlsafe(32)
            db.session.add(
                PasswordResetToken(
                    user_id=user.id,
                    token=token,
                    expires_at=datetime.utcnow() + timedelta(minutes=60),
                )
            )
            db.session.commit()
            reset_url = url_for("reset_password", token=token, _external=True)

            from services.email_helper import (
                is_email_configured, render_password_reset_email, send_email,
            )
            delivered = False
            if is_email_configured():
                subject, text, html = render_password_reset_email(
                    user_name=user.name or "", reset_url=reset_url
                )
                delivered = send_email(
                    to=user.email, subject=subject, body_text=text, body_html=html
                )

            # Dev fallback only: when SMTP isn't configured, surface
            # the URL inline so the flow can be tested without email
            # delivery. In a real deploy this branch never fires.
            if not delivered and not is_email_configured():
                flash(
                    f"SMTP isn't configured on this server — copy this reset link: {reset_url}",
                    "info",
                )
                return redirect(url_for("login"))

        flash(generic_flash, "success")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Land here from the email link. Show a simple new-password form
    and on submit, update the user's password + invalidate the token."""
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    record = (
        PasswordResetToken.query
        .filter_by(token=token, used_at=None)
        .filter(PasswordResetToken.expires_at > datetime.utcnow())
        .first()
    )
    if not record:
        flash(
            "This reset link is invalid or has expired. Request a new one.",
            "error",
        )
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if len(new) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("reset_password.html", token=token)
        if new != confirm:
            flash("Passwords don't match.", "error")
            return render_template("reset_password.html", token=token)

        user = db.session.get(User, record.user_id)
        if not user:
            flash("Account no longer exists.", "error")
            return redirect(url_for("login"))

        user.password_hash = generate_password_hash(new)
        record.used_at = datetime.utcnow()
        db.session.commit()
        flash("Password updated. You can now sign in with your new password.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        user = User.query.filter_by(email=email).first()

        if not user or not check_password_hash(user.password_hash, password):
            # Preserve the email so the user doesn't have to retype it
            # on the second attempt — never echo the password back.
            return render_template(
                "login.html",
                error="Invalid email or password.",
                form_email=email,
            )

        session.pop("_flashes", None)
        login_user(user)
        flash("Logged in successfully.", "success")
        return redirect(url_for("index"))

    return render_template("login.html", error=None, form_email="")


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

        if not has_enough_credits_for(current_user, "audit_run"):
            flash(
                "You don’t have enough credits to run another audit.",
                "warning",
            )
            return redirect(url_for("pricing_page"))

        if not spend_credits_for(current_user, "audit_run", notes="Client audit run"):
            flash(
                insufficient_credits_message(current_user, "audit_run", "An audit"),
                "warning",
            )
            return pricing_redirect_with_return_to()

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
            # Map known OpenAI exception types (RateLimitError,
            # APITimeoutError, etc.) to friendly user-facing copy
            # instead of leaking raw exception strings. Full traceback
            # is logged server-side for ops.
            from services.ai_errors import friendly_ai_error_message
            logger.exception(
                "Client audit failed for user_id=%s client_id=%s",
                current_user.id, client_id,
            )
            flash(friendly_ai_error_message(e), "error")
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
        return redirect(url_for("position_tracking_page"))

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

        if not has_enough_credits_for(current_user, "content_brief"):
            flash(
                "You don’t have enough credits to generate another brief.",
                "warning",
            )
            return redirect(url_for("pricing_page"))

        if not spend_credits_for(
            current_user, "content_brief", notes="Content brief generation"
        ):
            flash(
                insufficient_credits_message(current_user, "content_brief", "A brief"),
                "warning",
            )
            return pricing_redirect_with_return_to()

        # Prepend the structured Brand Kit to the brand_context blob so
        # the generator sees explicit voice / audience / differentiators.
        kit_block = brand_kit_context_block(client)
        if kit_block:
            brand_context = (
                f"{kit_block}\n\n{brand_context}" if brand_context else kit_block
            )

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

            # Auto-persist into the queue so the result page is the
            # detail view for a real item, not a transient render that
            # vanishes on next click. Audit finding #5.
            persisted_item = upsert_generation_item(
                client_id=client.get("id"),
                client_name=client.get("name", "Workspace"),
                target_query=target_query,
                content_type=content_type,
                item_type="brief",
                status="brief_generated",
                content=brief_text,
                title=f"Brief: {target_query}" if target_query else "Content Brief",
                user_id=current_user.id,
            )

            return render_template(
                "content_brief_result.html",
                result=result,
                client=client,
                aeo=aeo,
                top_competitors=top_competitors,
                tracked_prompt_count=len(tracked_rows),
                queue_item=persisted_item,
            )

        except Exception as e:
            refund_credits(
                current_user,
                1,
                notes="Refund for failed content brief generation",
            )
            from services.ai_errors import friendly_ai_error_message
            logger.exception(
                "Content brief generation failed for user_id=%s client_id=%s",
                current_user.id, client_id,
            )
            return render_template(
                "content_brief_form.html",
                client=client,
                error=friendly_ai_error_message(e),
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

        if not has_enough_credits_for(current_user, "content_draft"):
            flash(
                "You don’t have enough credits to generate another draft.",
                "warning",
            )
            return redirect(url_for("pricing_page"))

        if not spend_credits_for(
            current_user, "content_draft", notes="Content draft generation"
        ):
            flash(
                insufficient_credits_message(current_user, "content_draft", "A draft"),
                "warning",
            )
            return pricing_redirect_with_return_to()

        # Prepend the structured Brand Kit to brand_context.
        kit_block = brand_kit_context_block(client)
        if kit_block:
            brand_context = (
                f"{kit_block}\n\n{brand_context}" if brand_context else kit_block
            )

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

            draft_text = (
                result.get("draft", "")
                if isinstance(result, dict)
                else str(result)
            )

            # Auto-persist into the queue (or upsert into the brief
            # queue item this draft came from) so the result page is
            # the detail view for a real item. Audit finding #5.
            persisted_item = upsert_generation_item(
                client_id=client.get("id"),
                client_name=client.get("name", "Workspace"),
                target_query=target_query,
                content_type=content_type,
                item_type="draft",
                status="draft_generated",
                content=draft_text,
                title=f"Draft: {target_query}" if target_query else "Content Draft",
                user_id=current_user.id,
            )

            return render_template(
                "content_draft_result.html",
                client=client,
                result=result,
                queue_item=persisted_item,
            )

        except Exception as e:
            refund_credits(
                current_user,
                2,
                notes="Refund for failed content draft generation",
            )
            from services.ai_errors import friendly_ai_error_message
            logger.exception(
                "Content draft generation failed for user_id=%s client_id=%s",
                current_user.id, client_id,
            )
            return render_template(
                "content_draft_form.html",
                client=client,
                error=friendly_ai_error_message(e),
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

    # Pull the workspace context so the PDF can display the workspace
    # logo and industry alongside the audit data.
    client_payload = None
    summary_client_id = summary_data.get("client_id") if summary_data else None
    if summary_client_id:
        try:
            client_payload = get_client_by_id(str(summary_client_id))
        except Exception:
            client_payload = None

    html = render_template(
        "audit_summary_pdf.html",
        summary_filename=summary_filename,
        full_filename=full_filename,
        data=summary_data,
        report_date=report_date,
        client=client_payload,
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

    # Which workspaces have Wix / Framer connections — used to gate
    # the per-item "Publish to Wix" / "Publish to Framer" buttons.
    wix_connected_client_ids = {
        row.client_id
        for row in WixConnection.query.filter_by(user_id=current_user.id).all()
    }
    framer_connected_client_ids = {
        row.client_id
        for row in FramerConnection.query.filter_by(user_id=current_user.id).all()
    }

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
        wix_connected_client_ids=wix_connected_client_ids,
        framer_connected_client_ids=framer_connected_client_ids,
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


def _publish_queue_item_to_module_cms(item_id: str, *, platform: str):
    """Shared core for publish-to-Wix and publish-to-Framer routes.

    Looks up the queue item, verifies it's `ready` or `draft_generated`,
    finds the workspace's Wix/Framer connection, calls cms_publisher,
    and flashes the result. Mirrors the gating used by the Webflow
    publish path."""
    item = get_queue_item_by_id(item_id, user_id=current_user.id)
    if not item:
        abort(404)

    client_id = request.form.get("client_id", "").strip() or item.get("client_id")
    status = (item.get("status") or "").lower()
    if status not in {"ready", "draft_generated"}:
        flash(
            f"Approve the draft first — only ready items can publish to {platform.title()}.",
            "error",
        )
        return _redirect_to_queue(client_id)

    if platform == "wix":
        connection = WixConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).first()
    elif platform == "framer":
        connection = FramerConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).first()
    else:
        flash(f"Unknown publish platform: {platform}", "error")
        return _redirect_to_queue(client_id)

    if not connection:
        flash(
            f"No {platform.title()} connection for this workspace. "
            f"Connect one in the Module connectors page.",
            "error",
        )
        return _redirect_to_queue(client_id)

    target_collection = (request.form.get("collection_id") or "").strip() or None

    try:
        from services import cms_publisher
        if platform == "wix":
            result = cms_publisher.publish_to_wix(
                connection=connection, item=item, collection_id=target_collection
            )
        else:
            result = cms_publisher.publish_to_framer(
                connection=connection, item=item, collection_id=target_collection
            )
    except ValueError as exc:
        flash(str(exc), "error")
        return _redirect_to_queue(client_id)
    except Exception as exc:
        logger.warning("%s publish failed: %s", platform, exc)
        flash(f"Couldn't publish to {platform.title()}: {exc}", "error")
        return _redirect_to_queue(client_id)

    flash(
        f"Published as a draft on {platform.title()} (item ID {result.get('id', '?')}). "
        "Review and go live from your CMS.",
        "success",
    )
    return _redirect_to_queue(client_id)


@app.route("/content-queue/<item_id>/publish-to-wix", methods=["POST"])
@login_required
def publish_queue_item_to_wix(item_id):
    return _publish_queue_item_to_module_cms(item_id, platform="wix")


@app.route("/content-queue/<item_id>/publish-to-framer", methods=["POST"])
@login_required
def publish_queue_item_to_framer(item_id):
    return _publish_queue_item_to_module_cms(item_id, platform="framer")


def _parse_content_sections(text):
    """Split a content blob into addressable sections.

    Recognises markdown headings (#, ##, ###) and numbered headings like
    "1. Page Title" or "5. FAQ Section" — what the brief / draft generators
    actually produce. Each section spans from its heading line up to (but
    not including) the next heading. Returns a list of dicts:

        {idx, title, body, start, end, header_line}

    Where start/end are character offsets in the original text. If the text
    has no detectable headings, returns a single-section list covering the
    whole text with title "Document".
    """
    import re

    if not text:
        return []

    lines = text.split("\n")
    # Build line-start offsets so we can compute character ranges.
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1  # +1 for the newline

    md_header_re = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
    numbered_re = re.compile(r"^\s*(\d+)\.\s+(.{2,80})\s*$")
    # Treat **Bold Heading** lines (used in many of our drafts) as headings too,
    # but only if they're the entire line.
    bold_re = re.compile(r"^\s*\*\*(.+?)\*\*\s*$")

    headings = []
    for i, line in enumerate(lines):
        m = md_header_re.match(line)
        if m:
            headings.append({"line_idx": i, "title": m.group(2).strip(), "raw": line})
            continue
        m = numbered_re.match(line)
        if m:
            headings.append({"line_idx": i, "title": m.group(2).strip(), "raw": line})
            continue
        m = bold_re.match(line)
        if m:
            headings.append({"line_idx": i, "title": m.group(1).strip(), "raw": line})
            continue

    if not headings:
        return [{
            "idx": 0,
            "title": "Document",
            "body": text,
            "start": 0,
            "end": len(text),
            "header_line": None,
        }]

    sections = []
    for n, h in enumerate(headings):
        start_line = h["line_idx"]
        end_line = (
            headings[n + 1]["line_idx"] if n + 1 < len(headings) else len(lines)
        )
        start_char = offsets[start_line]
        end_char = offsets[end_line] if end_line < len(offsets) else len(text)
        body = text[start_char:end_char]
        sections.append({
            "idx": n,
            "title": h["title"][:80],
            "body": body,
            "start": start_char,
            "end": end_char,
            "header_line": h["raw"],
        })
    return sections


def _replace_section_text(full_text, sections, target_idx, replacement):
    """Patch a section's text back into the full document at its original
    range. If target_idx is out of bounds, returns the full_text unchanged."""
    if target_idx is None or not sections:
        return replacement
    if target_idx < 0 or target_idx >= len(sections):
        return full_text
    sec = sections[target_idx]
    before = full_text[: sec["start"]]
    after = full_text[sec["end"]:]
    # Make sure the replacement ends with a newline so the next section starts
    # on its own line.
    if replacement and not replacement.endswith("\n"):
        replacement = replacement + "\n"
    return before + replacement + after


@app.route("/content-queue/<item_id>/ai-edit/sections", methods=["GET"])
@login_required
def ai_edit_sections(item_id):
    """Return the parsed sections of a queue item's content for the
    section picker."""
    item = get_queue_item_by_id(item_id, user_id=current_user.id)
    if not item:
        return jsonify({"ok": False, "error": "Queue item not found."}), 404
    sections = _parse_content_sections(item.get("content") or "")
    return jsonify({
        "ok": True,
        "sections": [
            {"idx": s["idx"], "title": s["title"]}
            for s in sections
        ],
    })


@app.route("/content-queue/<item_id>/ai-edit/history", methods=["GET"])
@login_required
def ai_edit_history(item_id):
    """Return the persisted chat history + current content for the modal."""
    item = get_queue_item_by_id(item_id, user_id=current_user.id)
    if not item:
        return jsonify({"ok": False, "error": "Queue item not found."}), 404
    return jsonify({
        "ok": True,
        "current_content": item.get("content") or "",
        "chat_history": item.get("chat_history") or [],
    })


@app.route("/content-queue/<item_id>/ai-edit/clear", methods=["POST"])
@login_required
def ai_edit_clear_history(item_id):
    """Clear the persisted chat for an item."""
    if not clear_queue_item_chat_history(item_id, user_id=current_user.id):
        return jsonify({"ok": False, "error": "Queue item not found."}), 404
    return jsonify({"ok": True})


@app.route("/content-queue/<item_id>/ai-edit", methods=["POST"])
@login_required
def ai_edit_queue_item(item_id):
    """Multi-turn AI revision: append the user's instruction to the queue
    item's chat history, send the whole conversation as context, persist the
    AI's response with its proposed revised_content. Returns the full updated
    chat history so the modal can re-render."""
    item = get_queue_item_by_id(item_id, user_id=current_user.id)
    if not item:
        return jsonify({"ok": False, "error": "Queue item not found."}), 404

    instruction = (
        request.form.get("instruction")
        or (request.get_json(silent=True) or {}).get("instruction")
        or ""
    ).strip()
    if not instruction:
        return jsonify({"ok": False, "error": "Please describe the edit you want."}), 400

    if not has_enough_credits_for(current_user, "ai_edit_turn"):
        return jsonify({
            "ok": False,
            "error": (
                f"You need {get_action_cost('ai_edit_turn')} credit "
                "to send another AI edit. Top up to continue."
            ),
        }), 402

    # Optional section scope. -1 / None / blank = whole document.
    raw_target = (
        request.form.get("target_section_idx")
        or (request.get_json(silent=True) or {}).get("target_section_idx")
        or ""
    )
    try:
        target_section_idx = int(raw_target) if raw_target not in ("", None) else None
    except (TypeError, ValueError):
        target_section_idx = None
    if target_section_idx is not None and target_section_idx < 0:
        target_section_idx = None

    current_content = item.get("content") or ""
    if not current_content:
        return jsonify({"ok": False, "error": "This item has no content to edit yet. Generate a brief or draft first."}), 400

    if not os.getenv("OPENAI_API_KEY"):
        return jsonify({"ok": False, "error": "AI editing isn't set up on this site yet. Reach out to your admin."}), 503

    history = item.get("chat_history") or []

    # Use the latest revised content as the working document, falling back to
    # the queue item's content. This keeps the "section idx" stable across
    # turns since headings rarely shift.
    latest_content = current_content
    for entry in history:
        if entry.get("role") == "assistant" and entry.get("revised_content"):
            latest_content = entry["revised_content"]

    sections = _parse_content_sections(latest_content)
    target_section = None
    if target_section_idx is not None and 0 <= target_section_idx < len(sections):
        target_section = sections[target_section_idx]

    user_turn = {
        "role": "user",
        "content": instruction,
        "target_section_idx": target_section_idx,
        "target_section_title": (target_section or {}).get("title"),
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }

    try:
        from openai import OpenAI
        client = OpenAI()

        if target_section:
            system_prompt = (
                "You are an editor revising one specific section of a marketing document.\n"
                "The user gives instructions about that section only. Return strict JSON:\n"
                "  - revised_section: the full revised section (keep its heading line if it had one)\n"
                "  - summary: one short sentence on what you changed in this turn.\n"
                "Don't add other sections. Don't reformat the heading style — keep it as the original section had it. "
                "Don't add lorem ipsum. Make a best-effort revision if the instruction is fuzzy."
            )
            scoped_text = target_section["body"]
            scope_label = f"section \"{target_section['title']}\""
        else:
            system_prompt = (
                "You are an editor revising marketing content across multiple turns.\n"
                "The user gives instructions; you return a revised version of the content.\n"
                "Return strict JSON:\n"
                "  - revised_content: the full revised content, in the same format as the original\n"
                "  - summary: one short sentence on what you changed in this turn.\n"
                "Preserve any structured sections the original had. Don't add lorem ipsum. "
                "Make a best-effort revision if the instruction is fuzzy."
            )
            scoped_text = latest_content
            scope_label = "whole document"

        oai_messages = [{"role": "system", "content": system_prompt}]
        for entry in history:
            role = entry.get("role")
            if role == "user":
                tail = ""
                if entry.get("target_section_title"):
                    tail = f" [scope: {entry.get('target_section_title')}]"
                oai_messages.append({
                    "role": "user",
                    "content": (entry.get("content") or "") + tail,
                })
            elif role == "assistant":
                oai_messages.append({
                    "role": "assistant",
                    "content": entry.get("summary") or "(revised the content)",
                })

        oai_messages.append({
            "role": "user",
            "content": (
                f"NEW INSTRUCTION ({scope_label}): {instruction}\n\n"
                f"TEXT TO REVISE:\n{scoped_text}\n\n"
                "Return JSON with the right key per the system prompt."
            ),
        })

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=oai_messages,
            temperature=0.4,
        )

        raw = response.choices[0].message.content or "{}"
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}

        # Section-scoped responses come back under revised_section; whole-doc
        # under revised_content. Accept either to be forgiving.
        if target_section:
            revised_piece = (
                parsed.get("revised_section")
                or parsed.get("revised_content")
                or ""
            ).strip()
            if revised_piece:
                revised = _replace_section_text(
                    latest_content, sections, target_section_idx, revised_piece
                )
            else:
                revised = ""
        else:
            revised = (
                parsed.get("revised_content")
                or parsed.get("revised_section")
                or ""
            ).strip()

        summary = (parsed.get("summary") or "").strip() or "Revised the content."

        if not revised:
            # Persist just the user turn so the conversation stays consistent.
            append_queue_item_chat_messages(
                item_id, [user_turn], user_id=current_user.id
            )
            return jsonify({
                "ok": False,
                "error": "AI didn't return a usable revision. Try rephrasing your instruction.",
            }), 502

        assistant_turn = {
            "role": "assistant",
            "content": summary,
            "summary": summary,
            "revised_content": revised,
            "target_section_idx": target_section_idx,
            "target_section_title": (target_section or {}).get("title"),
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }

        updated = append_queue_item_chat_messages(
            item_id, [user_turn, assistant_turn], user_id=current_user.id
        )

        spend_credits_for(
            current_user,
            "ai_edit_turn",
            notes=f"AI edit turn: queue item {item_id}",
        )

        return jsonify({
            "ok": True,
            "current_content": current_content,
            "chat_history": (updated or {}).get("chat_history") or history + [user_turn, assistant_turn],
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

    if not has_enough_credits_for(current_user, "visual_generation"):
        flash(
            f"You need {get_action_cost('visual_generation')} credit to "
            "generate a visual. Top up to continue.",
            "warning",
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
            spend_credits_for(
                current_user,
                "visual_generation",
                notes=f"Visual generation: queue item {item_id}",
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

    # Auto-persist on generation already wrote a queue item (audit
    # #5). This route now upserts so the manual "Save edits" button
    # updates that same item with edited content instead of spawning
    # a duplicate. Older callers (legacy templates) still work — if
    # no matching item exists, upsert falls back to creating one.
    upsert_generation_item(
        client_id=client.get("id"),
        client_name=client.get("name"),
        target_query=target_query,
        content_type=content_type,
        item_type="brief",
        title=title,
        content=brief_text,
        status="brief_generated",
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

    # Same upsert pattern as save_generated_brief — the auto-persist
    # on draft generation (audit #5) already created the item, this
    # route now updates it with edited content from the textarea.
    upsert_generation_item(
        client_id=client.get("id"),
        client_name=client.get("name"),
        target_query=target_query,
        content_type=content_type,
        item_type="draft",
        title=title,
        content=draft_text,
        status="draft_generated",
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

        if not has_enough_credits_for(current_user, "audit_run"):
            flash(
                "You don’t have enough credits to run another audit.",
                "warning",
            )
            return redirect(url_for("pricing_page"))

        if not spend_credits_for(current_user, "audit_run", notes="New audit run"):
            flash(
                insufficient_credits_message(current_user, "audit_run", "An audit"),
                "warning",
            )
            return pricing_redirect_with_return_to()

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
            from services.ai_errors import friendly_ai_error_message
            logger.exception(
                "New audit failed for user_id=%s client_id=%s",
                current_user.id, client_id,
            )
            return render_template(
                "new_audit.html",
                clients=clients,
                error=friendly_ai_error_message(e),
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
    onboarding_state = {"active": False, "current_step": 1, "steps": []}

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
        onboarding_state = get_onboarding_state(current_user.id)

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
        "onboarding_state": onboarding_state,
        "can_run_audit": (
            current_user.is_authenticated
            and (
                has_unlimited_credits
                or credit_balance_numeric >= ACTION_CREDIT_COSTS["audit_run"]
            )
        ),
        "can_generate_brief": (
            current_user.is_authenticated
            and (
                has_unlimited_credits
                or credit_balance_numeric >= ACTION_CREDIT_COSTS["content_brief"]
            )
        ),
        "can_generate_draft": (
            current_user.is_authenticated
            and (
                has_unlimited_credits
                or credit_balance_numeric >= ACTION_CREDIT_COSTS["content_draft"]
            )
        ),
        "action_credit_costs": ACTION_CREDIT_COSTS,
        "agency_brand": effective_agency_branding(),
        # Cache buster for the main stylesheet — appended as ?v=<mtime>
        # so Chrome / Safari serve the new CSS the moment the file is
        # touched, instead of holding onto a multi-day-cached copy.
        "static_css_version": _static_css_version(),
        # Single source of truth for score-band thresholds. Templates
        # call {% set band = score_band(score) %} and read band.label /
        # band.note / band.pill_class. Replaces the four-different-
        # threshold-sets situation where dashboard / client_detail /
        # client_visibility / audit_summary each rolled their own
        # cutoffs (a 65-score workspace was "Strong" on one page and
        # "Moderate" on another).
        "score_band": score_band,
    }


def score_band(score):
    """Single source of truth for AEO/visibility score → band label
    + interpretation copy + pill class. Used across dashboard,
    workspace overview, visibility module, and audit summary.

    Canonical thresholds (matches help_content.opportunity_level
    glossary entry):
      ≥ 75 → strong       (Low Opportunity)
      ≥ 50 → moderate     (Moderate Opportunity)
      < 50 → opportunity  (High Opportunity)
    """
    try:
        s = float(score or 0)
    except (TypeError, ValueError):
        s = 0.0

    if s >= 75:
        return {
            "band": "strong",
            "label": "Strong visibility",
            "note": "Strong visibility — hold the lead and expand to new query clusters.",
            "pill_class": "score-pill score-pill-success",
            "opportunity": "Low Opportunity",
        }
    if s >= 50:
        return {
            "band": "moderate",
            "label": "Moderate visibility",
            "note": "Developing — close the biggest pillar gap to move the score up.",
            "pill_class": "score-pill score-pill-warning",
            "opportunity": "Moderate Opportunity",
        }
    return {
        "band": "opportunity",
        "label": "High opportunity",
        "note": "Significant upside — re-audit, fill content gaps, and start tracking the queries you want to win.",
        "pill_class": "score-pill score-pill-opportunity",
        "opportunity": "High Opportunity",
    }


def _static_css_version() -> str:
    """File mtime of the main stylesheet, used as a CSS cache-buster.
    Falls back to a constant when the file is missing so we never
    crash a render."""
    try:
        path = os.path.join(
            app.static_folder or "static", "styles-v2.1.css"
        )
        return str(int(os.path.getmtime(path)))
    except Exception:
        return "1"


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

    user_plan = (
        getattr(current_user, "plan", "free")
        if current_user.is_authenticated
        else "free"
    )

    workspace_count_used = 0
    workspace_base_limit = workspace_limit_for_plan(user_plan)
    workspace_total_limit = get_workspace_limit(current_user) if current_user.is_authenticated else 0
    if current_user.is_authenticated:
        try:
            workspace_count_used = get_workspace_count(current_user.id)
        except Exception:
            workspace_count_used = 0

    from pricing import plan_allows_seat_addon, seat_limit_for_plan
    team_seats_base = seat_limit_for_plan(user_plan)
    team_seats_total = (
        get_seat_limit(current_user) if current_user.is_authenticated else 1
    )
    team_seats_used = (
        count_team_members(current_user.id)
        if current_user.is_authenticated and not current_user.team_owner_id
        else 1
    )
    team_active_members = []
    team_pending_invites = []
    if current_user.is_authenticated and not current_user.team_owner_id:
        try:
            team_active_members = (
                User.query.filter_by(team_owner_id=current_user.id)
                .order_by(User.created_at.desc())
                .all()
            )
            team_pending_invites = (
                TeamInvite.query.filter_by(
                    owner_user_id=current_user.id, status="pending"
                )
                .order_by(TeamInvite.invited_at.desc())
                .all()
            )
        except Exception:
            pass

    context = {
        "active_settings_section": section,
        "is_internal_user": is_internal_user,
        "view_mode": session.get("dev_view_mode", "auto"),
        "referral_link": referral_link,
        "credit_history": credit_history,
        "referrals_made": referrals_made,
        "topup_bundles": get_bundles_for_plan(user_plan),
        "is_subscriber": is_subscriber(user_plan),
        "baseline_credit_price": baseline_credit_price(user_plan),
        "action_credit_costs": ACTION_CREDIT_COSTS,
        "workspace_count_used": workspace_count_used,
        "workspace_base_limit": workspace_base_limit,
        "workspace_total_limit": workspace_total_limit,
        "plan_allows_addon": plan_allows_workspace_addon(user_plan),
        "extra_workspace_addon_price_usd": EXTRA_WORKSPACE_ADDON_PRICE_USD,
        "plan_allows_seats": plan_allows_seat_addon(user_plan),
        "team_seats_base": team_seats_base,
        "team_seats_total": team_seats_total,
        "team_seats_used": team_seats_used,
        "team_active_members": team_active_members,
        "team_pending_invites": team_pending_invites,
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


@app.route("/settings/profile/update", methods=["POST"])
@login_required
def settings_update_profile():
    """Update the user's display name. Email is treated as immutable for
    now since it's also the login identifier — changing it warrants a
    re-verification flow we haven't built."""
    new_name = (request.form.get("name") or "").strip()
    if not new_name:
        flash("Name can't be empty.", "error")
        return redirect(url_for("settings_page"))
    if len(new_name) > 200:
        flash("Name is too long.", "error")
        return redirect(url_for("settings_page"))
    current_user.name = new_name
    db.session.commit()
    flash("Display name updated.", "success")
    return redirect(url_for("settings_page"))


@app.route("/settings/white-label/update", methods=["POST"])
@login_required
def settings_update_white_label():
    """Save the agency white-label fields and toggle. Logo upload is
    a separate route so users can save text-only changes without
    reuploading. Free users can edit fields but the toggle stays
    off until they're on a paid plan — keeps the upgrade nudge clean."""
    enable = request.form.get("enable") == "on"
    name = (request.form.get("agency_name") or "").strip()[:255]
    tagline = (request.form.get("agency_tagline") or "").strip()[:255]
    website = (request.form.get("agency_website") or "").strip()[:500]
    footer = (request.form.get("agency_footer") or "").strip()[:500]
    disclaimer = (request.form.get("agency_disclaimer") or "").strip()[:500]

    current_user.agency_name = name or None
    current_user.agency_tagline = tagline or None
    current_user.agency_website = website or None
    current_user.agency_footer = footer or None
    current_user.agency_disclaimer = disclaimer or None
    current_user.is_white_label_enabled = bool(enable)
    db.session.commit()
    flash("White-label branding saved.", "success")
    return redirect(url_for("settings_white_label"))


@app.route("/settings/white-label/upload-logo", methods=["POST"])
@login_required
def settings_upload_agency_logo():
    """Save an uploaded agency logo. Routes through the storage layer
    (S3 when configured, local disk otherwise) so multi-instance
    deploys keep logos available across replicas."""
    from services.storage import logo_storage

    file = request.files.get("logo")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("settings_white_label"))

    ext = (file.filename.rsplit(".", 1)[-1] or "").lower()
    if ext not in {"png", "jpg", "jpeg", "svg", "webp"}:
        flash("Use PNG, JPG, SVG, or WEBP.", "error")
        return redirect(url_for("settings_white_label"))

    new_name = f"agency-{current_user.id}-{secrets.token_hex(4)}.{ext}"
    try:
        logo_storage.save("agency_logos", new_name, file)
    except Exception as exc:
        logger.warning("Agency logo upload failed: %s", exc)
        flash("Could not save the logo. Try again.", "error")
        return redirect(url_for("settings_white_label"))

    # Best-effort cleanup of the previous file (works on either backend).
    if current_user.agency_logo_filename:
        logo_storage.delete("agency_logos", current_user.agency_logo_filename)

    current_user.agency_logo_filename = new_name
    db.session.commit()
    flash("Agency logo uploaded.", "success")
    return redirect(url_for("settings_white_label"))


@app.route("/settings/white-label/remove-logo", methods=["POST"])
@login_required
def settings_remove_agency_logo():
    from services.storage import logo_storage
    if current_user.agency_logo_filename:
        logo_storage.delete("agency_logos", current_user.agency_logo_filename)
        current_user.agency_logo_filename = None
        db.session.commit()
        flash("Agency logo removed.", "success")
    return redirect(url_for("settings_white_label"))


@app.route("/settings/white-label")
@login_required
def settings_white_label():
    return render_settings_section("white_label")


@app.route("/settings/account/change-password", methods=["POST"])
@login_required
def settings_change_password():
    """Change the user's password. Requires the current password to
    avoid drive-by changes if a session is hijacked."""
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""

    if not check_password_hash(current_user.password_hash, current):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("settings_account"))
    if len(new) < 8:
        flash("New password must be at least 8 characters.", "error")
        return redirect(url_for("settings_account"))
    if new != confirm:
        flash("New password and confirmation don't match.", "error")
        return redirect(url_for("settings_account"))

    current_user.password_hash = generate_password_hash(new)
    db.session.commit()
    flash("Password updated.", "success")
    return redirect(url_for("settings_account"))


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


# =========================
# Shopify integration routes
# =========================
# Connect a workspace to a Shopify store via OAuth, then pull products via
# the Admin REST API. Persists the access token in `shopify_connections`
# so subsequent audits can read store data without re-asking the user.


def _shopify_redirect_uri() -> str:
    """The OAuth redirect URI Shopify will call back into.

    Must match the value registered on the app in the Shopify Partners
    dashboard. Allow override for local dev via env, fall back to the
    request host."""
    override = os.getenv("SHOPIFY_REDIRECT_URI")
    if override:
        return override
    return url_for("shopify_oauth_callback", _external=True)


@app.route("/integrations/shopify/connect/<int:client_id>")
@login_required
def shopify_connect(client_id):
    """Kick off the Shopify OAuth install for a workspace.

    Expects ?shop=foo.myshopify.com (or just ?shop=foo). Redirects the
    user to Shopify's authorization screen."""
    from services.shopify_client import (
        ShopifyConfigError,
        build_install_url,
        is_shopify_configured,
    )

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != current_user.id:
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    if not is_shopify_configured():
        flash(
            "Shopify is not yet configured on this server. "
            "Set SHOPIFY_API_KEY and SHOPIFY_API_SECRET to enable store connections.",
            "error",
        )
        return redirect(url_for("client_detail", client_id=client_id))

    shop = (request.args.get("shop") or "").strip()
    if not shop:
        flash("Please enter your store URL (e.g. my-store.myshopify.com).", "error")
        return redirect(url_for("client_detail", client_id=client_id))

    state = secrets.token_urlsafe(24)
    session["shopify_oauth_state"] = state
    session["shopify_oauth_client_id"] = client_id

    try:
        install_url = build_install_url(
            shop=shop,
            redirect_uri=_shopify_redirect_uri(),
            state=state,
        )
    except ShopifyConfigError as exc:
        flash(str(exc), "error")
        return redirect(url_for("client_detail", client_id=client_id))

    return redirect(install_url)


@app.route("/integrations/shopify/callback")
@login_required
def shopify_oauth_callback():
    """Handle the Shopify OAuth callback.

    Verifies HMAC, exchanges the temporary code for an access token,
    and persists a ShopifyConnection row scoped to the user + workspace
    that initiated the install."""
    from services.shopify_client import (
        ShopifyAdminClient,
        ShopifyAPIError,
        ShopifyConfigError,
        _normalize_shop_domain,
        exchange_code_for_token,
        verify_hmac,
    )

    params = {k: v for k, v in request.args.items()}

    expected_state = session.pop("shopify_oauth_state", None)
    pending_client_id = session.pop("shopify_oauth_client_id", None)
    if not expected_state or params.get("state") != expected_state:
        flash("Shopify install state mismatch — please retry the connection.", "error")
        return redirect(url_for("index"))

    if not verify_hmac(params):
        flash("Shopify HMAC verification failed.", "error")
        return redirect(url_for("index"))

    workspace = db.session.get(Client, pending_client_id) if pending_client_id else None
    if not workspace or workspace.user_id != current_user.id:
        flash("The workspace this install was started from could not be found.", "error")
        return redirect(url_for("index"))

    shop_domain = _normalize_shop_domain(params.get("shop") or "")
    code = params.get("code")
    if not shop_domain or not code:
        flash("Missing shop or code in Shopify callback.", "error")
        return redirect(url_for("client_detail", client_id=workspace.id))

    try:
        token_payload = exchange_code_for_token(shop_domain, code)
    except (ShopifyConfigError, ShopifyAPIError) as exc:
        flash(f"Could not finish Shopify install: {exc}", "error")
        return redirect(url_for("client_detail", client_id=workspace.id))

    access_token = token_payload.get("access_token")
    if not access_token:
        flash("Shopify did not return an access token.", "error")
        return redirect(url_for("client_detail", client_id=workspace.id))

    shop_meta: Dict[str, Any] = {}
    try:
        admin = ShopifyAdminClient(shop_domain, access_token)
        shop_meta = admin.get_shop()
    except ShopifyAPIError as exc:
        logger.warning("Shopify get_shop after install failed: %s", exc)

    existing = (
        ShopifyConnection.query.filter_by(
            user_id=current_user.id, client_id=workspace.id
        ).one_or_none()
    )
    if existing:
        existing.shop_domain = shop_domain
        existing.access_token = access_token
        existing.scope = token_payload.get("scope")
        existing.shop_meta = shop_meta or existing.shop_meta
        existing.updated_at = datetime.utcnow()
    else:
        db.session.add(
            ShopifyConnection(
                user_id=current_user.id,
                client_id=workspace.id,
                shop_domain=shop_domain,
                access_token=access_token,
                scope=token_payload.get("scope"),
                shop_meta=shop_meta or None,
            )
        )
    db.session.commit()

    flash(f"Connected Shopify store {shop_domain}.", "success")
    return redirect(url_for("shopify_products", client_id=workspace.id))


def _refresh_shopify_findings(connection: "ShopifyConnection", products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run the catalog audit, persist the findings on the connection, and
    return them. Persisting the findings (not the full product list) keeps
    the calendar render path fast: subsequent renders just read shop_meta."""
    from services.shopify_audit import find_shopify_action_items, summarize_catalog

    findings = find_shopify_action_items(products)
    summary = summarize_catalog(products)

    meta = dict(connection.shop_meta or {})
    meta["cached_findings"] = findings
    meta["cached_summary"] = summary
    meta["cached_findings_at"] = datetime.utcnow().isoformat()
    connection.shop_meta = meta
    flag_modified(connection, "shop_meta")
    return findings


def _shopify_findings_for_client(user_id: int, client_id: Optional[int]) -> List[Dict[str, Any]]:
    """Return cached Shopify findings for a workspace (empty list if none).

    Reads the shop_meta payload set by the products view / sync route, so
    no HTTP round-trip happens on the render path."""
    if not client_id:
        return []
    try:
        connection = (
            ShopifyConnection.query.filter_by(user_id=user_id, client_id=client_id)
            .one_or_none()
        )
    except Exception:
        return []
    if not connection or not connection.shop_meta:
        return []
    findings = connection.shop_meta.get("cached_findings") if isinstance(connection.shop_meta, dict) else None
    if isinstance(findings, list):
        return findings
    return []


@app.route("/integrations/shopify/sync/<int:client_id>", methods=["POST"])
@login_required
def shopify_sync_products(client_id):
    """Pull the latest product list from the connected store."""
    from services.shopify_client import ShopifyAdminClient, ShopifyAPIError

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != current_user.id:
        return jsonify({"success": False, "message": "Workspace not found."}), 404

    connection = (
        ShopifyConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).one_or_none()
    )
    if not connection:
        return jsonify(
            {"success": False, "message": "No Shopify store connected for this workspace."}
        ), 400

    try:
        admin = ShopifyAdminClient(connection.shop_domain, connection.access_token)
        products = admin.list_products(limit=50)
    except ShopifyAPIError as exc:
        return jsonify({"success": False, "message": str(exc)}), 502

    findings = _refresh_shopify_findings(connection, products)
    connection.last_synced_at = datetime.utcnow()
    db.session.commit()

    return jsonify(
        {
            "success": True,
            "shop_domain": connection.shop_domain,
            "count": len(products),
            "findings_count": len(findings),
            "synced_at": connection.last_synced_at.isoformat(),
        }
    )


@app.route("/integrations/shopify/products/<int:client_id>")
@login_required
def shopify_products(client_id):
    """Render the connected store's products for an audit-ready view."""
    from services.shopify_client import ShopifyAdminClient, ShopifyAPIError

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != current_user.id:
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    connection = (
        ShopifyConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).one_or_none()
    )

    from services.shopify_client import scope_has

    products: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {}
    error: Optional[str] = None
    has_write_scope = bool(connection and scope_has(connection.scope, "write_products"))
    if connection:
        try:
            admin = ShopifyAdminClient(connection.shop_domain, connection.access_token)
            products = admin.list_products(limit=50)
            findings = _refresh_shopify_findings(connection, products)
            summary = (connection.shop_meta or {}).get("cached_summary", {}) if isinstance(connection.shop_meta, dict) else {}
            connection.last_synced_at = datetime.utcnow()
            db.session.commit()
        except ShopifyAPIError as exc:
            error = str(exc)
            findings = _shopify_findings_for_client(current_user.id, client_id)
            summary = (connection.shop_meta or {}).get("cached_summary", {}) if isinstance(connection.shop_meta, dict) else {}

    description_proposals = []
    if connection and isinstance(connection.shop_meta, dict):
        description_proposals = connection.shop_meta.get("cached_description_proposals") or []

    return render_template(
        "integrations/shopify_products.html",
        workspace=workspace,
        connection=connection,
        products=products,
        findings=findings,
        summary=summary,
        error=error,
        has_write_scope=has_write_scope,
        description_proposals=description_proposals,
        description_rewrite_cost=get_action_cost("description_rewrite_batch"),
    )


def _generate_alt_text(product: Dict[str, Any], image_index: int) -> str:
    """Build a short, descriptive alt text from the product fields.

    Strategy: lead with product title (the strongest known signal), append
    vendor/type if present and not already in the title. Used as a fast,
    deterministic fallback when AI generation isn't available."""
    title = (product.get("title") or "").strip()
    vendor = (product.get("vendor") or "").strip()
    product_type = (product.get("product_type") or "").strip()

    parts = [title or "Product image"]
    extras = []
    title_lower = title.lower()
    if product_type and product_type.lower() not in title_lower:
        extras.append(product_type)
    if vendor and vendor.lower() not in title_lower:
        extras.append(f"by {vendor}")
    if extras:
        parts.append(" ".join(extras))
    base = " — ".join(parts)
    if image_index > 0:
        base = f"{base} (view {image_index + 1})"
    return base[:240]


def _generate_alt_text_ai(
    product: Dict[str, Any],
    image_url: str,
    image_index: int,
) -> Optional[str]:
    """Ask gpt-4o-mini (vision) to describe the product image in one
    factual sentence, grounded in the product context.

    Returns None on any failure — caller is responsible for falling
    back to the template-based generator."""
    if not os.getenv("OPENAI_API_KEY") or not image_url:
        return None

    title = (product.get("title") or "").strip()
    vendor = (product.get("vendor") or "").strip()
    product_type = (product.get("product_type") or "").strip()

    context_parts = [f"Product title: {title or 'unknown'}"]
    if product_type:
        context_parts.append(f"Product type: {product_type}")
    if vendor:
        context_parts.append(f"Vendor: {vendor}")
    context = " | ".join(context_parts)

    try:
        from openai import OpenAI
        client = OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            max_tokens=80,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write concise, factual image alt text for ecommerce product photos. "
                        "Lead with the product, then describe visible attributes (color, material, "
                        "shape, key features). One sentence, under 25 words, no marketing language, "
                        "no leading 'image of' or 'photo of'. Plain text only."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Context: {context}"},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ],
        )
        alt = (response.choices[0].message.content or "").strip()
        # Strip trailing punctuation that some models add and quotes
        alt = alt.strip().strip('"').strip("'")
        if not alt:
            return None
        if image_index > 0:
            alt = f"{alt} (view {image_index + 1})"
        return alt[:240]
    except Exception as exc:
        logger.warning("AI alt-text generation failed: %s", exc)
        return None


@app.route("/integrations/shopify/fix/alt-text/<int:client_id>", methods=["POST"])
@login_required
def shopify_fix_alt_text(client_id):
    """Auto-fill missing alt text on every product image in the store.

    Safe write-back: alt is purely additive metadata, the route only
    touches images whose alt is empty, and Shopify retains the previous
    value in image history if the user wants to revert."""
    from services.shopify_client import (
        ShopifyAdminClient,
        ShopifyAPIError,
        scope_has,
    )

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != current_user.id:
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    connection = (
        ShopifyConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).one_or_none()
    )
    if not connection:
        flash("No Shopify store connected for this workspace.", "error")
        return redirect(url_for("client_detail", client_id=client_id))

    if not scope_has(connection.scope, "write_products"):
        flash(
            "This store was connected before write access was enabled. "
            "Please reconnect the store to grant the write_products scope.",
            "error",
        )
        return redirect(url_for("shopify_products", client_id=client_id))

    if not has_enough_credits_for(current_user, "alt_text_fix_batch"):
        flash(
            f"You need {get_action_cost('alt_text_fix_batch')} credits to run "
            "an alt-text fix. Top up to continue.",
            "warning",
        )
        return redirect(url_for("shopify_products", client_id=client_id))

    admin = ShopifyAdminClient(connection.shop_domain, connection.access_token)
    try:
        products = admin.list_products(limit=50)
    except ShopifyAPIError as exc:
        flash(f"Could not load products: {exc}", "error")
        return redirect(url_for("shopify_products", client_id=client_id))

    patched = 0
    failed = 0
    for product in products:
        product_id = product.get("id")
        if not product_id:
            continue
        images = product.get("images") or []
        for idx, image in enumerate(images):
            if not isinstance(image, dict):
                continue
            existing_alt = (image.get("alt") or "").strip()
            if existing_alt:
                continue
            image_id = image.get("id")
            if not image_id:
                continue
            image_src = (image.get("src") or "").strip()
            alt_text = (
                _generate_alt_text_ai(product, image_src, idx)
                or _generate_alt_text(product, idx)
            )
            try:
                admin.update_product_image_alt(product_id, image_id, alt_text)
                patched += 1
            except ShopifyAPIError as exc:
                logger.warning(
                    "Shopify alt-text PUT failed for product %s image %s: %s",
                    product_id, image_id, exc,
                )
                failed += 1

    if patched:
        try:
            refreshed = admin.list_products(limit=50)
            _refresh_shopify_findings(connection, refreshed)
            connection.last_synced_at = datetime.utcnow()
            db.session.commit()
        except ShopifyAPIError as exc:
            logger.warning("Refresh after alt-text fix failed: %s", exc)

    ai_used = bool(os.getenv("OPENAI_API_KEY"))
    source_label = "AI-generated alt text" if ai_used else "alt text"

    if patched:
        spend_credits_for(
            current_user,
            "alt_text_fix_batch",
            notes=f"Alt-text fix: {patched} images",
        )

    if patched and not failed:
        flash(f"Filled {source_label} on {patched} product image{'s' if patched != 1 else ''}.", "success")
    elif patched and failed:
        flash(
            f"Filled alt text on {patched} image{'s' if patched != 1 else ''}; "
            f"{failed} update{'s' if failed != 1 else ''} failed.",
            "warning",
        )
    elif failed:
        flash(f"Could not update any images ({failed} failures).", "error")
    else:
        flash("No images needed alt text — your catalog is already covered.", "info")

    return redirect(url_for("shopify_products", client_id=client_id))


# =========================
# AI Answer Monitor routes
# =========================
# Track how the brand surfaces in AI answer engines over time. Each
# check writes a PromptCheckSnapshot row; the monitor page renders a
# sparkline-style history per tracked prompt, plus a "Run all checks
# now" button that re-runs every prompt for the workspace.


def _run_answer_check_for_id(
    prompt_id: int, brand_name: str
) -> Optional[List[Dict[str, Any]]]:
    """Internal helper that loads a prompt row and runs a check across
    every enabled AI engine.

    Returns the list of per-engine snapshots, or None when the prompt
    isn't found or doesn't belong to the current user. An empty list
    means no engines were enabled or every backend errored out."""
    from ai_answer_agent import enabled_engines, simulate_ai_answer
    from services.answer_monitor import run_answer_check

    row = PromptTracking.query.filter_by(
        id=prompt_id, user_id=current_user.id
    ).one_or_none()
    if not row:
        return None

    engines = enabled_engines() or ["chatgpt"]
    try:
        return run_answer_check(
            db=db,
            PromptTracking=PromptTracking,
            PromptCheckSnapshot=PromptCheckSnapshot,
            simulate_ai_answer=simulate_ai_answer,
            prompt_row=row,
            brand_name=brand_name,
            engines=engines,
        )
    except Exception as exc:
        logger.warning("Answer check failed for prompt %s: %s", prompt_id, exc)
        return None


@app.route("/answer-monitor")
@login_required
def answer_monitor_page():
    """Render the AI Answer Monitor for the selected workspace."""
    from services.answer_monitor import (
        load_history_for_prompts,
        summarize_history,
    )

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

    domain = ""
    if selected_client:
        domain = normalize_website(selected_client.get("website", "")) or ""

    rows: List[PromptTracking] = []
    if domain:
        rows = (
            PromptTracking.query.filter_by(
                user_id=current_user.id, domain=domain
            )
            .order_by(PromptTracking.created_at.desc())
            .all()
        )

    from ai_answer_agent import enabled_engines, engine_label, ENGINE_REGISTRY

    history = load_history_for_prompts(
        db=db,
        PromptCheckSnapshot=PromptCheckSnapshot,
        prompt_ids=[r.id for r in rows],
    )
    summary = summarize_history(history)

    enabled = enabled_engines() or ["chatgpt"]
    enabled_labels = [engine_label(e) for e in enabled]

    prompt_views: List[Dict[str, Any]] = []
    for row in rows:
        per_engine_history = history.get(row.id, {})
        engines_in_view: List[Dict[str, Any]] = []
        for slug, snapshots in per_engine_history.items():
            latest = snapshots[-1] if snapshots else None
            engines_in_view.append(
                {
                    "slug": slug,
                    "label": engine_label(slug),
                    "kind": (ENGINE_REGISTRY.get(slug) or {}).get("kind", "trained"),
                    "history": snapshots,
                    "latest": latest,
                }
            )
        # Stable order: registered engines first (in registry order), then any extras.
        order = list(ENGINE_REGISTRY.keys())
        engines_in_view.sort(
            key=lambda e: (order.index(e["slug"]) if e["slug"] in order else 99)
        )

        # Pick a representative latest for the headline citation pill.
        latest_overall = None
        for ev in engines_in_view:
            if ev["latest"] and (
                latest_overall is None
                or ev["latest"]["score"] > latest_overall["score"]
            ):
                latest_overall = ev["latest"]

        prompt_views.append(
            {
                "id": row.id,
                "prompt": row.prompt,
                "platform": row.platform or "AI assistant",
                "topic": row.topic,
                "last_checked": row.last_checked,
                "change": row.change,
                "mentioned": row.mentioned,
                "score": row.prompt_score,
                "score_band": row.score_band,
                "brand_position": row.brand_position,
                "top_competitor": row.top_competitor,
                "engines": engines_in_view,
                "latest": latest_overall,
            }
        )

    return render_template(
        "answer_monitor.html",
        clients=clients,
        selected_client=selected_client,
        prompts=prompt_views,
        summary=summary,
        domain=domain,
        enabled_engines=enabled,
        enabled_engine_labels=enabled_labels,
    )


@app.route("/answer-monitor/run-all", methods=["POST"])
@login_required
def answer_monitor_run_all():
    """Re-run every tracked prompt in the selected workspace."""
    client_id = request.form.get("client_id", "").strip()
    selected_client = get_client_by_id(client_id) if client_id else None
    if not selected_client:
        flash("Workspace not found.", "error")
        return redirect(url_for("answer_monitor_page"))

    domain = normalize_website(selected_client.get("website", "")) or ""
    if not domain:
        flash("This workspace has no website set, so AI answer checks can't run.", "error")
        return redirect(url_for("answer_monitor_page", client_id=client_id))

    brand_name = (selected_client.get("name") or "").strip() or domain
    rows = (
        PromptTracking.query.filter_by(user_id=current_user.id, domain=domain)
        .all()
    )
    if not rows:
        flash("No tracked prompts to check yet for this workspace.", "info")
        return redirect(url_for("answer_monitor_page", client_id=client_id))

    if not has_enough_credits_for(current_user, "answer_monitor_run_all"):
        flash(
            f"You need {get_action_cost('answer_monitor_run_all')} credits "
            "to re-run every prompt. Top up to continue.",
            "warning",
        )
        return redirect(url_for("answer_monitor_page", client_id=client_id))

    succeeded = 0
    failed = 0
    total_snapshots = 0
    engines_seen: set = set()
    for row in rows:
        result = _run_answer_check_for_id(row.id, brand_name)
        if not result:
            failed += 1
        else:
            succeeded += 1
            total_snapshots += len(result)
            for snap in result:
                if snap.get("engine_label"):
                    engines_seen.add(snap["engine_label"])

    engines_label = (
        " across " + ", ".join(sorted(engines_seen)) if engines_seen else ""
    )

    if succeeded:
        spend_credits_for(
            current_user,
            "answer_monitor_run_all",
            notes=f"Answer monitor sweep: {succeeded} prompts × {len(engines_seen) or 1} engines",
        )

    if succeeded and not failed:
        flash(
            f"Checked {succeeded} prompt{'s' if succeeded != 1 else ''}{engines_label} "
            f"({total_snapshots} snapshots saved).",
            "success",
        )
    elif succeeded and failed:
        flash(
            f"Checked {succeeded}; {failed} failed. Verify OPENAI_API_KEY / PERPLEXITY_API_KEY.",
            "warning",
        )
    else:
        flash(
            "Could not run any checks. Set OPENAI_API_KEY (and optionally PERPLEXITY_API_KEY).",
            "error",
        )

    return redirect(url_for("answer_monitor_page", client_id=client_id))


@app.route("/answer-monitor/run/<int:prompt_id>", methods=["POST"])
@login_required
def answer_monitor_run_single(prompt_id):
    """Re-run a single tracked prompt."""
    row = PromptTracking.query.filter_by(
        id=prompt_id, user_id=current_user.id
    ).one_or_none()
    if not row:
        flash("Tracked prompt not found.", "error")
        return redirect(url_for("answer_monitor_page"))

    selected_client = None
    if row.domain:
        for client in build_client_views():
            if normalize_website(client.get("website", "")) == row.domain:
                selected_client = client
                break

    brand_name = (
        (selected_client.get("name") if selected_client else None)
        or row.domain
        or "this brand"
    )

    if not has_enough_credits_for(current_user, "answer_monitor_run_single"):
        flash(
            f"You need {get_action_cost('answer_monitor_run_single')} credit "
            "to re-run that check. Top up to continue.",
            "warning",
        )
        redirect_client_id = (
            str(selected_client.get("id")) if selected_client else ""
        )
        return redirect(
            url_for("answer_monitor_page", client_id=redirect_client_id)
        )

    result = _run_answer_check_for_id(prompt_id, brand_name)
    if not result:
        flash("Could not run that check. Verify OPENAI_API_KEY is set.", "error")
    else:
        spend_credits_for(
            current_user,
            "answer_monitor_run_single",
            notes=f"Answer monitor: prompt #{prompt_id}",
        )
        cited_engines = [
            snap["engine_label"] for snap in result if snap["brand_mentioned"]
        ]
        if cited_engines:
            flash(
                f"Cited in {', '.join(cited_engines)}.",
                "success",
            )
        else:
            flash(
                f"Not cited in {', '.join(snap['engine_label'] for snap in result)}.",
                "warning",
            )

    redirect_client_id = (
        str(selected_client.get("id")) if selected_client else ""
    )
    return redirect(
        url_for("answer_monitor_page", client_id=redirect_client_id)
    )


# =========================
# Marketplace presence audits
# =========================
# Track AI visibility for the user's storefronts on third-party
# marketplaces (Etsy, Amazon, Shopee, eBay). We don't ingest the
# catalog — we generate marketplace-flavoured prompts and check how
# often the shop is cited.


@app.route("/marketplace-audits/<int:client_id>")
@login_required
def marketplace_audits_page(client_id):
    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != current_user.id:
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    presences = (
        MarketplacePresence.query
        .filter_by(user_id=current_user.id, client_id=client_id)
        .order_by(MarketplacePresence.created_at.desc())
        .all()
    )
    return render_template(
        "marketplace_audits.html",
        workspace=workspace,
        presences=presences,
        marketplace_options=[
            ("etsy", "Etsy"),
            ("amazon", "Amazon"),
            ("shopee", "Shopee"),
            ("ebay", "eBay"),
            ("tiktok_shop", "TikTok Shop"),
            ("lazada", "Lazada"),
            ("other", "Other"),
        ],
        marketplace_audit_cost=get_action_cost("marketplace_audit"),
    )


@app.route("/marketplace-audits/<int:client_id>/add", methods=["POST"])
@login_required
def marketplace_add_presence(client_id):
    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != current_user.id:
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    marketplace = (request.form.get("marketplace") or "").strip().lower()
    shop_url = (request.form.get("shop_url") or "").strip()
    shop_name = (request.form.get("shop_name") or "").strip()
    category = (request.form.get("category") or "").strip()
    region = (request.form.get("region") or "").strip()

    if marketplace not in {"etsy", "amazon", "shopee", "ebay", "other"} or not shop_url:
        flash("Please pick a marketplace and paste your shop URL.", "error")
        return redirect(url_for("marketplace_audits_page", client_id=client_id))

    presence = MarketplacePresence(
        user_id=current_user.id,
        client_id=client_id,
        marketplace=marketplace,
        shop_name=shop_name or None,
        shop_url=shop_url,
        category=category or None,
        region=region or None,
    )
    db.session.add(presence)
    db.session.commit()
    flash(f"Linked {presence.shop_name or presence.shop_url}.", "success")
    return redirect(url_for("marketplace_audits_page", client_id=client_id))


@app.route("/marketplace-audits/<int:client_id>/run/<int:presence_id>", methods=["POST"])
@login_required
def marketplace_run_audit(client_id, presence_id):
    """Run the marketplace audit for one presence row."""
    from ai_answer_agent import enabled_engines, simulate_ai_answer
    from services.marketplace_audit import run_marketplace_audit

    presence = MarketplacePresence.query.filter_by(
        id=presence_id, user_id=current_user.id, client_id=client_id
    ).one_or_none()
    if not presence:
        flash("That marketplace listing wasn't found.", "error")
        return redirect(url_for("marketplace_audits_page", client_id=client_id))

    if not has_enough_credits_for(current_user, "marketplace_audit"):
        flash(
            f"You need {get_action_cost('marketplace_audit')} credits to "
            "run a marketplace audit.",
            "warning",
        )
        return redirect(url_for("marketplace_audits_page", client_id=client_id))

    engines = enabled_engines() or ["chatgpt"]
    try:
        payload = run_marketplace_audit(
            presence=presence,
            simulate_ai_answer=simulate_ai_answer,
            engines=engines,
        )
    except Exception as exc:
        logger.warning("Marketplace audit failed: %s", exc)
        flash("Could not run that marketplace audit. Verify OPENAI_API_KEY.", "error")
        return redirect(url_for("marketplace_audits_page", client_id=client_id))

    presence.last_audit_payload = payload
    presence.last_visibility_score = payload.get("visibility_score")
    presence.last_audited_at = datetime.utcnow()
    db.session.commit()

    spend_credits_for(
        current_user,
        "marketplace_audit",
        notes=f"Marketplace audit: {presence.marketplace}/{presence.shop_name or presence.shop_url}",
    )
    flash(
        f"Audit complete — {payload['visibility_score']}% visibility across "
        f"{len(payload['queries'])} queries.",
        "success",
    )
    return redirect(url_for("marketplace_audits_page", client_id=client_id))


@app.route("/marketplace-audits/<int:client_id>/delete/<int:presence_id>", methods=["POST"])
@login_required
def marketplace_delete_presence(client_id, presence_id):
    presence = MarketplacePresence.query.filter_by(
        id=presence_id, user_id=current_user.id, client_id=client_id
    ).one_or_none()
    if presence:
        db.session.delete(presence)
        db.session.commit()
        flash("Marketplace listing removed.", "success")
    return redirect(url_for("marketplace_audits_page", client_id=client_id))


# =========================
# Google Search Console integration
# =========================
# OAuth + Search Analytics pulls for verified GSC properties. Pro and
# Growth plans only — Free sees an upgrade nudge on the
# workspace card.


def _gsc_redirect_uri() -> str:
    """OAuth redirect URI Google calls back into. Match the value
    registered on the OAuth client in the Google Cloud console."""
    override = os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
    if override:
        return override
    return url_for("gsc_oauth_callback", _external=True)


def _ensure_gsc_access_token(connection) -> str:
    """Return a valid access token for this connection, refreshing
    silently if the cached one is expired or about to expire."""
    from services.gsc_client import GSCAPIError, refresh_access_token

    now = datetime.utcnow()
    expires_at = connection.token_expires_at
    if expires_at and expires_at - now > timedelta(seconds=60):
        return connection.access_token

    if not connection.refresh_token:
        raise GSCAPIError(
            "Access token expired and no refresh token is on file. Reconnect Google Search Console."
        )
    payload = refresh_access_token(connection.refresh_token)
    connection.access_token = payload.get("access_token") or connection.access_token
    expires_in = int(payload.get("expires_in") or 3600)
    connection.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    db.session.commit()
    return connection.access_token


@app.route("/integrations/gsc/connect/<int:client_id>")
@login_required
def gsc_connect(client_id):
    """Kick off the Google Search Console OAuth install for a workspace."""
    from pricing import plan_allows_google_search_console
    from services.gsc_client import (
        GSCConfigError,
        build_install_url,
        is_gsc_configured,
    )

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    owner = effective_owner() or current_user
    if not plan_allows_google_search_console(owner.plan):
        flash(
            "The Google Search Console connector is available on Pro and Growth plans.",
            "warning",
        )
        return redirect(url_for("pricing_page"))

    if not is_gsc_configured():
        flash(
            "Google Search Console isn't configured on this server yet — "
            "ask your admin to set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
            "error",
        )
        return redirect(url_for("client_detail", client_id=client_id))

    state = secrets.token_urlsafe(24)
    session["gsc_oauth_state"] = state
    session["gsc_oauth_client_id"] = client_id

    try:
        url = build_install_url(redirect_uri=_gsc_redirect_uri(), state=state)
    except GSCConfigError as exc:
        flash(str(exc), "error")
        return redirect(url_for("client_detail", client_id=client_id))

    return redirect(url)


@app.route("/integrations/gsc/callback")
@login_required
def gsc_oauth_callback():
    """Handle the OAuth callback — exchange code, persist tokens."""
    from services.gsc_client import (
        GSCAPIError,
        GSCConfigError,
        exchange_code_for_token,
    )

    expected_state = session.pop("gsc_oauth_state", None)
    pending_client_id = session.pop("gsc_oauth_client_id", None)
    if not expected_state or request.args.get("state") != expected_state:
        flash("Google Search Console state mismatch — please retry.", "error")
        return redirect(url_for("index"))

    error = request.args.get("error")
    if error:
        flash(f"Google declined the connection: {error}", "error")
        return redirect(url_for("index"))

    code = request.args.get("code")
    if not code:
        flash("Missing authorization code from Google.", "error")
        return redirect(url_for("index"))

    workspace = db.session.get(Client, pending_client_id) if pending_client_id else None
    if not workspace or workspace.user_id != effective_owner_id():
        flash("The workspace this install was started from could not be found.", "error")
        return redirect(url_for("index"))

    try:
        token_payload = exchange_code_for_token(
            code=code, redirect_uri=_gsc_redirect_uri()
        )
    except (GSCConfigError, GSCAPIError) as exc:
        flash(f"Could not finish Google install: {exc}", "error")
        return redirect(url_for("client_detail", client_id=workspace.id))

    access = token_payload.get("access_token")
    refresh = token_payload.get("refresh_token")
    scope = token_payload.get("scope")
    expires_in = int(token_payload.get("expires_in") or 3600)
    if not access:
        flash("Google did not return an access token.", "error")
        return redirect(url_for("client_detail", client_id=workspace.id))

    owner_id = effective_owner_id() or current_user.id
    existing = (
        GoogleSearchConsoleConnection.query.filter_by(
            user_id=owner_id, client_id=workspace.id
        ).one_or_none()
    )
    if existing:
        existing.access_token = access
        if refresh:
            existing.refresh_token = refresh
        existing.scope = scope
        existing.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
        existing.updated_at = datetime.utcnow()
    else:
        db.session.add(
            GoogleSearchConsoleConnection(
                user_id=owner_id,
                client_id=workspace.id,
                access_token=access,
                refresh_token=refresh,
                scope=scope,
                token_expires_at=datetime.utcnow() + timedelta(seconds=expires_in),
            )
        )
    db.session.commit()

    flash("Connected Google Search Console. Pick a property to track below.", "success")
    return redirect(url_for("gsc_dashboard", client_id=workspace.id))


def _refresh_gsc_payload(connection) -> Dict[str, Any]:
    """Pull aggregate KPIs + top queries + top pages for the connected
    site. Cached on shop_meta-style fields so the dashboard renders
    HTTP-free between syncs."""
    from services.gsc_client import GSCClient

    if not connection.site_url:
        return {}

    access = _ensure_gsc_access_token(connection)
    client = GSCClient(access)

    end = datetime.utcnow().date()
    start = end - timedelta(days=28)
    start_s = start.isoformat()
    end_s = end.isoformat()

    totals = client.query_search_analytics(
        site_url=connection.site_url,
        start_date=start_s,
        end_date=end_s,
        dimensions=None,
        row_limit=1,
    )
    top_queries = client.query_search_analytics(
        site_url=connection.site_url,
        start_date=start_s,
        end_date=end_s,
        dimensions=["query"],
        row_limit=10,
    )
    top_pages = client.query_search_analytics(
        site_url=connection.site_url,
        start_date=start_s,
        end_date=end_s,
        dimensions=["page"],
        row_limit=10,
    )

    summary = {
        "site_url": connection.site_url,
        "range_start": start_s,
        "range_end": end_s,
        "totals": totals[0] if totals else {},
        "top_queries": top_queries,
        "top_pages": top_pages,
        "fetched_at": datetime.utcnow().isoformat(),
    }
    connection.last_sync_payload = summary
    connection.last_synced_at = datetime.utcnow()
    flag_modified(connection, "last_sync_payload")
    db.session.commit()
    return summary


@app.route("/integrations/gsc/<int:client_id>")
@login_required
def gsc_dashboard(client_id):
    """Workspace-scoped Search Console dashboard."""
    from pricing import plan_allows_google_search_console
    from services.gsc_client import GSCAPIError, GSCClient, is_gsc_configured

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    owner = effective_owner() or current_user
    if not plan_allows_google_search_console(owner.plan):
        flash(
            "The Google Search Console connector is available on Pro and Growth plans.",
            "warning",
        )
        return redirect(url_for("pricing_page"))

    connection = (
        GoogleSearchConsoleConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )

    sites: List[Dict[str, Any]] = []
    error: Optional[str] = None
    if connection:
        try:
            access = _ensure_gsc_access_token(connection)
            sites = GSCClient(access).list_sites()
        except GSCAPIError as exc:
            error = str(exc)

    summary = (connection.last_sync_payload or {}) if connection else {}

    return render_template(
        "integrations/gsc_dashboard.html",
        workspace=workspace,
        connection=connection,
        sites=sites,
        summary=summary,
        error=error,
        gsc_configured=is_gsc_configured(),
    )


@app.route("/integrations/gsc/<int:client_id>/select-site", methods=["POST"])
@login_required
def gsc_select_site(client_id):
    """Save the picked GSC property and pull a first sync."""
    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    connection = (
        GoogleSearchConsoleConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if not connection:
        flash("No Google Search Console connection on this workspace.", "error")
        return redirect(url_for("client_detail", client_id=client_id))

    site_url = (request.form.get("site_url") or "").strip()
    if not site_url:
        flash("Please pick a property.", "error")
        return redirect(url_for("gsc_dashboard", client_id=client_id))

    connection.site_url = site_url
    db.session.commit()

    from services.gsc_client import GSCAPIError
    try:
        _refresh_gsc_payload(connection)
        flash(f"Tracking {site_url}. Latest 28 days of data loaded.", "success")
    except GSCAPIError as exc:
        flash(f"Picked {site_url}, but couldn't load metrics: {exc}", "warning")

    return redirect(url_for("gsc_dashboard", client_id=client_id))


@app.route("/integrations/gsc/<int:client_id>/sync", methods=["POST"])
@login_required
def gsc_sync(client_id):
    """Re-pull the last 28 days of data."""
    from services.gsc_client import GSCAPIError

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        return jsonify({"success": False, "message": "Workspace not found."}), 404

    connection = (
        GoogleSearchConsoleConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if not connection or not connection.site_url:
        flash("Pick a property first.", "warning")
        return redirect(url_for("gsc_dashboard", client_id=client_id))

    try:
        _refresh_gsc_payload(connection)
        flash("Search Console metrics refreshed.", "success")
    except GSCAPIError as exc:
        flash(f"Sync failed: {exc}", "error")

    return redirect(url_for("gsc_dashboard", client_id=client_id))


# =========================
# Cal.com booking integration
# =========================


@app.route("/client/<int:client_id>/upload-logo", methods=["POST"])
@login_required
def upload_workspace_logo(client_id):
    """Save an uploaded workspace logo. Routes through the storage
    layer (S3 when configured, local disk otherwise) so the same code
    path serves single-instance dev and multi-instance production."""
    from services.storage import logo_storage

    workspace = (
        Client.query.filter_by(id=client_id, user_id=effective_owner_id())
        .one_or_none()
    )
    if not workspace:
        abort(404)

    file = request.files.get("logo")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("client_detail", client_id=client_id))

    ext = (file.filename.rsplit(".", 1)[-1] or "").lower()
    if ext not in {"png", "jpg", "jpeg", "svg", "webp"}:
        flash("Use PNG, JPG, SVG, or WEBP.", "error")
        return redirect(url_for("client_detail", client_id=client_id))

    new_name = f"workspace-{workspace.id}-{secrets.token_hex(4)}.{ext}"
    try:
        logo_storage.save("workspace_logos", new_name, file)
    except Exception as exc:
        logger.warning("Workspace logo upload failed: %s", exc)
        flash("Could not save the logo. Try again.", "error")
        return redirect(url_for("client_detail", client_id=client_id))

    if workspace.logo_filename:
        logo_storage.delete("workspace_logos", workspace.logo_filename)
    workspace.logo_filename = new_name
    db.session.commit()
    flash("Workspace logo updated. It'll appear on the audit PDF and dashboard.", "success")
    return redirect(url_for("client_detail", client_id=client_id))


@app.route("/client/<int:client_id>/remove-logo", methods=["POST"])
@login_required
def remove_workspace_logo(client_id):
    from services.storage import logo_storage

    workspace = (
        Client.query.filter_by(id=client_id, user_id=effective_owner_id())
        .one_or_none()
    )
    if not workspace:
        abort(404)
    if workspace.logo_filename:
        logo_storage.delete("workspace_logos", workspace.logo_filename)
        workspace.logo_filename = None
        db.session.commit()
        flash("Workspace logo removed.", "success")
    return redirect(url_for("client_detail", client_id=client_id))


@app.route("/client/<int:client_id>/refresh-profile", methods=["POST"])
@login_required
def refresh_business_profile(client_id):
    """Manually re-run the Tavily-driven business profile enrichment.
    Auto-runs lazily when the audit PDF is opened, but an explicit
    button is useful when the user has updated their website / Google
    Business Profile and wants the next PDF to reflect that."""
    workspace = (
        Client.query.filter_by(id=client_id, user_id=effective_owner_id())
        .one_or_none()
    )
    if not workspace:
        abort(404)
    try:
        from services.business_profile_research import research_business_profile
        data = research_business_profile(
            name=workspace.name,
            website=workspace.website,
            location=workspace.location,
        ) or {}
        if data.get("founded_year"):
            workspace.founded_year = data["founded_year"]
        if data.get("google_rating"):
            workspace.google_rating = data["google_rating"]
        if data.get("google_review_count") is not None:
            workspace.google_review_count = data["google_review_count"]
        if data.get("executive_summary"):
            workspace.business_summary = data["executive_summary"]
        if data.get("core_services") and not workspace.brand_services:
            workspace.brand_services = data["core_services"]
        workspace.business_profile_updated_at = datetime.utcnow()
        db.session.commit()
        flash("Business profile refreshed from web research.", "success")
    except Exception as exc:
        logger.warning("Business profile refresh failed: %s", exc)
        flash(
            "Couldn't refresh the profile. Make sure TAVILY_API_KEY and OPENAI_API_KEY are set.",
            "error",
        )
    return redirect(url_for("client_detail", client_id=client_id))


@app.route("/client/<int:client_id>/brand-kit", methods=["GET", "POST"])
@login_required
def client_brand_kit(client_id):
    """Brand Kit Studio — structured brand fields with a live preview.

    Per the blueprint, Brand Kit → Preview is foundational
    architecture: structured brand direction dramatically improves
    generation quality across audits, content drafts, AI editing, and
    website generation. We persist colors / typography / voice /
    audience / differentiators as discrete columns so downstream
    steps can lift values directly instead of parsing a notes blob."""
    workspace = (
        Client.query.filter_by(id=client_id, user_id=effective_owner_id())
        .one_or_none()
    )
    if not workspace:
        abort(404)

    if request.method == "POST":
        workspace.brand_audience = (request.form.get("brand_audience") or "").strip() or None
        workspace.brand_services = (request.form.get("brand_services") or "").strip() or None
        workspace.brand_differentiators = (request.form.get("brand_differentiators") or "").strip() or None
        workspace.brand_voice = (request.form.get("brand_voice") or "").strip()[:255] or None
        workspace.brand_personality = (request.form.get("brand_personality") or "").strip()[:255] or None
        workspace.brand_avoid = (request.form.get("brand_avoid") or "").strip() or None
        workspace.brand_primary_color = (request.form.get("brand_primary_color") or "").strip()[:20] or None
        workspace.brand_secondary_color = (request.form.get("brand_secondary_color") or "").strip()[:20] or None
        workspace.brand_accent_color = (request.form.get("brand_accent_color") or "").strip()[:20] or None
        workspace.brand_typography = (request.form.get("brand_typography") or "").strip()[:120] or None
        workspace.brand_imagery_direction = (request.form.get("brand_imagery_direction") or "").strip() or None
        workspace.brand_kit_updated_at = datetime.utcnow()
        # Any save invalidates the prior approval so generators don't
        # lean on stale direction. The user re-approves once they're
        # happy with the new state.
        workspace.brand_kit_approved_at = None
        db.session.commit()
        flash("Brand Kit saved. Click Approve when you're ready to use it across generators.", "success")
        return redirect(url_for("client_brand_kit", client_id=client_id))

    return render_template("brand_kit_studio.html", workspace=workspace)


@app.route("/client/<int:client_id>/brand-kit/approve", methods=["POST"])
@login_required
def client_brand_kit_approve(client_id):
    """Mark the Brand Kit as approved. Generators will lift kit values
    only when this timestamp is set + newer than the last edit."""
    workspace = (
        Client.query.filter_by(id=client_id, user_id=effective_owner_id())
        .one_or_none()
    )
    if not workspace:
        abort(404)
    workspace.brand_kit_approved_at = datetime.utcnow()
    db.session.commit()
    flash("Brand Kit approved — generators will now lift these values into output.", "success")
    return redirect(url_for("client_brand_kit", client_id=client_id))


@app.route("/client/<int:client_id>/brand-kit/unapprove", methods=["POST"])
@login_required
def client_brand_kit_unapprove(client_id):
    """Withdraw approval — generators fall back to user-typed
    brand_context only until re-approved."""
    workspace = (
        Client.query.filter_by(id=client_id, user_id=effective_owner_id())
        .one_or_none()
    )
    if not workspace:
        abort(404)
    workspace.brand_kit_approved_at = None
    db.session.commit()
    flash("Brand Kit set back to draft.", "info")
    return redirect(url_for("client_brand_kit", client_id=client_id))


@app.route("/integrations/calcom/<int:client_id>")
@login_required
def calcom_dashboard(client_id):
    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))
    connection = (
        CalComConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    payload = (connection.last_payload or {}) if connection else {}
    return render_template(
        "integrations/calcom_dashboard.html",
        workspace=workspace,
        connection=connection,
        payload=payload,
    )


@app.route("/integrations/calcom/<int:client_id>/connect", methods=["POST"])
@login_required
def calcom_connect(client_id):
    from services.calcom_client import (
        CalComAPIError, CalComClient, CalComConfigError, summarize,
    )

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    api_key = (request.form.get("api_key") or "").strip()
    username = (request.form.get("username") or "").strip().lstrip("@")
    if not api_key or not username:
        flash("API key and Cal.com username are both required.", "error")
        return redirect(url_for("calcom_dashboard", client_id=client_id))

    try:
        client = CalComClient(api_key, username)
        payload = summarize(client)
    except (CalComConfigError, CalComAPIError) as exc:
        flash(f"Could not connect Cal.com: {exc}", "error")
        return redirect(url_for("calcom_dashboard", client_id=client_id))

    existing = (
        CalComConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if existing:
        existing.api_key = api_key
        existing.username = username
        existing.last_payload = payload
        existing.last_synced_at = datetime.utcnow()
        existing.updated_at = datetime.utcnow()
    else:
        db.session.add(
            CalComConnection(
                user_id=effective_owner_id() or current_user.id,
                client_id=client_id,
                api_key=api_key,
                username=username,
                last_payload=payload,
                last_synced_at=datetime.utcnow(),
            )
        )
    db.session.commit()
    flash(f"Connected Cal.com for @{username}.", "success")
    return redirect(url_for("calcom_dashboard", client_id=client_id))


@app.route("/integrations/calcom/<int:client_id>/sync", methods=["POST"])
@login_required
def calcom_sync(client_id):
    from services.calcom_client import CalComAPIError, CalComClient, summarize

    connection = (
        CalComConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if not connection:
        flash("No Cal.com connection on this workspace.", "error")
        return redirect(url_for("calcom_dashboard", client_id=client_id))
    try:
        client = CalComClient(connection.api_key, connection.username)
        payload = summarize(client)
        connection.last_payload = payload
        connection.last_synced_at = datetime.utcnow()
        flag_modified(connection, "last_payload")
        db.session.commit()
        flash("Cal.com data refreshed.", "success")
    except CalComAPIError as exc:
        flash(f"Sync failed: {exc}", "error")
    return redirect(url_for("calcom_dashboard", client_id=client_id))


@app.route("/integrations/calcom/<int:client_id>/disconnect", methods=["POST"])
@login_required
def calcom_disconnect(client_id):
    connection = (
        CalComConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if connection:
        db.session.delete(connection)
        db.session.commit()
        flash("Disconnected Cal.com.", "success")
    return redirect(url_for("client_detail", client_id=client_id))


# =========================
# WooCommerce integration
# =========================
# Key-based, read-only. Reuses services.shopify_audit by normalising
# WooCommerce products into the Shopify-shaped dict the audit expects.


def _refresh_woocommerce_findings(connection, products):
    """Run the catalog audit on normalised products and persist."""
    from services.shopify_audit import find_shopify_action_items, summarize_catalog
    from services.woocommerce_client import normalize_woo_product_to_shopify_shape

    normalised = [normalize_woo_product_to_shopify_shape(p) for p in (products or [])]
    findings = find_shopify_action_items(normalised)
    summary = summarize_catalog(normalised)

    payload = {
        "store_url": connection.store_url,
        "summary": summary,
        "findings": findings,
        "fetched_at": datetime.utcnow().isoformat(),
    }
    connection.last_audit_payload = payload
    connection.last_synced_at = datetime.utcnow()
    flag_modified(connection, "last_audit_payload")
    return payload


@app.route("/integrations/woocommerce/<int:client_id>")
@login_required
def woocommerce_dashboard(client_id):
    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    connection = (
        WooCommerceConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    payload = (connection.last_audit_payload or {}) if connection else {}

    return render_template(
        "integrations/woocommerce_dashboard.html",
        workspace=workspace,
        connection=connection,
        payload=payload,
    )


@app.route("/integrations/woocommerce/<int:client_id>/connect", methods=["POST"])
@login_required
def woocommerce_connect(client_id):
    """Save store URL + consumer key/secret and pull a first audit."""
    from services.woocommerce_client import (
        WooAPIError, WooClient, WooConfigError, _normalize_store_url,
    )

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    raw_url = (request.form.get("store_url") or "").strip()
    ck = (request.form.get("consumer_key") or "").strip()
    cs = (request.form.get("consumer_secret") or "").strip()
    store_url = _normalize_store_url(raw_url)

    if not store_url or not ck or not cs:
        flash("Store URL, consumer key, and consumer secret are all required.", "error")
        return redirect(url_for("woocommerce_dashboard", client_id=client_id))

    try:
        client = WooClient(store_url, ck, cs)
        client.shop_summary()  # ping + auth check
        products = client.list_products(limit=50)
    except (WooConfigError, WooAPIError) as exc:
        flash(f"Could not connect to WooCommerce: {exc}", "error")
        return redirect(url_for("woocommerce_dashboard", client_id=client_id))

    existing = (
        WooCommerceConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if existing:
        existing.store_url = store_url
        existing.consumer_key = ck
        existing.consumer_secret = cs
        existing.updated_at = datetime.utcnow()
        connection = existing
    else:
        connection = WooCommerceConnection(
            user_id=effective_owner_id() or current_user.id,
            client_id=client_id,
            store_url=store_url,
            consumer_key=ck,
            consumer_secret=cs,
        )
        db.session.add(connection)
        db.session.flush()

    _refresh_woocommerce_findings(connection, products)
    db.session.commit()

    flash(f"Connected WooCommerce store {store_url}.", "success")
    return redirect(url_for("woocommerce_dashboard", client_id=client_id))


@app.route("/integrations/woocommerce/<int:client_id>/sync", methods=["POST"])
@login_required
def woocommerce_sync(client_id):
    from services.woocommerce_client import WooAPIError, WooClient

    connection = (
        WooCommerceConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if not connection:
        flash("No WooCommerce store connected.", "error")
        return redirect(url_for("woocommerce_dashboard", client_id=client_id))

    try:
        client = WooClient(
            connection.store_url, connection.consumer_key, connection.consumer_secret
        )
        products = client.list_products(limit=50)
        _refresh_woocommerce_findings(connection, products)
        db.session.commit()
        flash("WooCommerce catalog refreshed.", "success")
    except WooAPIError as exc:
        flash(f"Sync failed: {exc}", "error")
    return redirect(url_for("woocommerce_dashboard", client_id=client_id))


@app.route("/integrations/woocommerce/<int:client_id>/disconnect", methods=["POST"])
@login_required
def woocommerce_disconnect(client_id):
    connection = (
        WooCommerceConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if connection:
        db.session.delete(connection)
        db.session.commit()
        flash("Disconnected WooCommerce store.", "success")
    return redirect(url_for("client_detail", client_id=client_id))


@app.route("/integrations/ga/<int:client_id>")
@login_required
def ga_dashboard(client_id):
    """GA4 dashboard for a workspace. Reuses the GSC connection row so
    the user only goes through Google OAuth once for both products."""
    from pricing import plan_allows_google_search_console
    from services.ga_client import GA4Client, GAAPIError

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    owner = effective_owner() or current_user
    if not plan_allows_google_search_console(owner.plan):
        flash(
            "Google Analytics is available on Pro and Growth plans.",
            "warning",
        )
        return redirect(url_for("pricing_page"))

    connection = (
        GoogleSearchConsoleConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )

    properties: List[Dict[str, Any]] = []
    error: Optional[str] = None
    if connection:
        try:
            access = _ensure_gsc_access_token(connection)
            properties = GA4Client(access).list_properties()
        except GAAPIError as exc:
            error = str(exc)

    payload = (connection.ga_payload or {}) if connection else {}

    return render_template(
        "integrations/ga_dashboard.html",
        workspace=workspace,
        connection=connection,
        properties=properties,
        payload=payload,
        error=error,
    )


@app.route("/integrations/ga/<int:client_id>/select-property", methods=["POST"])
@login_required
def ga_select_property(client_id):
    """Pick a GA4 property and pull a first 28-day summary."""
    from services.ga_client import GA4Client, GAAPIError, summarize_property

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    connection = (
        GoogleSearchConsoleConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if not connection:
        flash("Connect Google first from the Search Console page.", "warning")
        return redirect(url_for("gsc_dashboard", client_id=client_id))

    property_id = (request.form.get("property_id") or "").strip()
    if not property_id:
        flash("Please pick a property.", "error")
        return redirect(url_for("ga_dashboard", client_id=client_id))

    connection.ga_property_id = property_id
    db.session.commit()

    try:
        access = _ensure_gsc_access_token(connection)
        payload = summarize_property(GA4Client(access), property_id=property_id)
        connection.ga_payload = payload
        connection.ga_synced_at = datetime.utcnow()
        flag_modified(connection, "ga_payload")
        db.session.commit()
        flash(
            f"Tracking GA4 property {property_id}. Last 28 days loaded.",
            "success",
        )
    except GAAPIError as exc:
        flash(f"Picked property, but GA query failed: {exc}", "warning")

    return redirect(url_for("ga_dashboard", client_id=client_id))


@app.route("/integrations/ga/<int:client_id>/sync", methods=["POST"])
@login_required
def ga_sync(client_id):
    """Re-pull the 28-day GA summary."""
    from services.ga_client import GA4Client, GAAPIError, summarize_property

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        return jsonify({"success": False, "message": "Workspace not found."}), 404

    connection = (
        GoogleSearchConsoleConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if not connection or not connection.ga_property_id:
        flash("Pick a GA property first.", "warning")
        return redirect(url_for("ga_dashboard", client_id=client_id))

    try:
        access = _ensure_gsc_access_token(connection)
        payload = summarize_property(
            GA4Client(access), property_id=connection.ga_property_id
        )
        connection.ga_payload = payload
        connection.ga_synced_at = datetime.utcnow()
        flag_modified(connection, "ga_payload")
        db.session.commit()
        flash("Google Analytics metrics refreshed.", "success")
    except GAAPIError as exc:
        flash(f"GA sync failed: {exc}", "error")

    return redirect(url_for("ga_dashboard", client_id=client_id))


@app.route("/integrations/ga/<int:client_id>/disconnect", methods=["POST"])
@login_required
def ga_disconnect(client_id):
    """Remove the GA property selection only (keeps the GSC half intact)."""
    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    connection = (
        GoogleSearchConsoleConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if connection:
        connection.ga_property_id = None
        connection.ga_payload = None
        connection.ga_synced_at = None
        db.session.commit()
        flash("Disconnected Google Analytics for this workspace.", "success")
    return redirect(url_for("client_detail", client_id=client_id))


@app.route("/integrations/gsc/<int:client_id>/disconnect", methods=["POST"])
@login_required
def gsc_disconnect(client_id):
    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != effective_owner_id():
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    connection = (
        GoogleSearchConsoleConnection.query.filter_by(
            user_id=effective_owner_id(), client_id=client_id
        ).one_or_none()
    )
    if connection:
        db.session.delete(connection)
        db.session.commit()
        flash("Disconnected Google Search Console.", "success")
    else:
        flash("No Google Search Console connection on this workspace.", "info")
    return redirect(url_for("client_detail", client_id=client_id))


@app.route("/cron/answer-monitor", methods=["POST", "GET"])
def cron_answer_monitor():
    """Scheduled sweep that re-runs the AI Answer Monitor for every
    paid user whose youngest snapshot is older than 6 days.

    Auth: pass a `CRON_SECRET` either as `X-Cron-Secret` header or
    `?secret=…` query param. Mismatched secrets get a 403.

    Idempotent: a workspace is skipped if its newest snapshot is < 6
    days old, so calling the endpoint daily costs nothing extra. The
    sweep does NOT deduct credits — the auto-run is a subscriber
    benefit; manual re-runs from the UI still charge.

    Suggested cron entry (server-side):
        0 8 * * 1  curl -fsS -X POST -H "X-Cron-Secret: $SECRET" \\
                       https://your-host/cron/answer-monitor
    """
    from ai_answer_agent import enabled_engines, simulate_ai_answer
    from services.answer_monitor import run_answer_check

    expected = os.getenv("CRON_SECRET")
    if not expected:
        return jsonify({"ok": False, "error": "CRON_SECRET not configured"}), 503
    provided = (
        request.headers.get("X-Cron-Secret")
        or request.args.get("secret")
        or ""
    )
    if provided != expected:
        abort(403)

    from pricing import SUBSCRIBER_PLANS

    users = User.query.filter(User.plan.in_(list(SUBSCRIBER_PLANS - {"dev_unlimited"}))).all()
    engines = enabled_engines() or ["chatgpt"]

    workspaces_run = 0
    workspaces_skipped = 0
    snapshots_total = 0
    failures = 0
    cutoff = datetime.utcnow() - timedelta(days=6)

    for user in users:
        clients = Client.query.filter_by(user_id=user.id).all()
        for client in clients:
            domain = normalize_website(client.website or "")
            if not domain:
                continue
            prompts = (
                PromptTracking.query
                .filter_by(user_id=user.id, domain=domain)
                .all()
            )
            if not prompts:
                continue
            youngest = (
                db.session.query(db.func.max(PromptCheckSnapshot.checked_at))
                .filter(PromptCheckSnapshot.prompt_tracking_id.in_([p.id for p in prompts]))
                .scalar()
            )
            if youngest and youngest > cutoff:
                workspaces_skipped += 1
                continue

            brand_name = (client.name or domain).strip() or "this brand"
            ran_for_workspace = False
            for prompt in prompts:
                try:
                    snaps = run_answer_check(
                        db=db,
                        PromptTracking=PromptTracking,
                        PromptCheckSnapshot=PromptCheckSnapshot,
                        simulate_ai_answer=simulate_ai_answer,
                        prompt_row=prompt,
                        brand_name=brand_name,
                        engines=engines,
                    )
                    snapshots_total += len(snaps)
                    ran_for_workspace = True
                except Exception as exc:
                    logger.warning(
                        "Cron answer-monitor failed for prompt %s: %s",
                        prompt.id, exc,
                    )
                    failures += 1
            if ran_for_workspace:
                workspaces_run += 1

    return jsonify(
        {
            "ok": True,
            "workspaces_run": workspaces_run,
            "workspaces_skipped": workspaces_skipped,
            "snapshots_saved": snapshots_total,
            "failures": failures,
            "engines": engines,
        }
    )


def _generate_product_description_ai(product: Dict[str, Any]) -> Optional[str]:
    """Ask gpt-4o-mini for a richer product description grounded in the
    product's title, vendor, type, and any existing thin copy. Returns
    HTML body (safe-ish, no scripts) or None on failure."""
    if not os.getenv("OPENAI_API_KEY"):
        return None

    title = (product.get("title") or "").strip()
    vendor = (product.get("vendor") or "").strip()
    product_type = (product.get("product_type") or "").strip()
    tags = product.get("tags")
    if isinstance(tags, list):
        tag_text = ", ".join(tags[:8])
    else:
        tag_text = str(tags or "")[:200]

    existing = (product.get("body_html") or "").strip()
    # Strip any tags from existing for the prompt context.
    import re as _re
    existing_text = _re.sub(r"<[^>]+>", " ", existing)
    existing_text = _re.sub(r"\s+", " ", existing_text).strip()[:400]

    user_prompt = (
        "Write a richer product description in clean HTML for the product below. "
        "Lead with one short sentence that answers what the product is and who it's "
        "for. Add a 2-3 sentence paragraph covering materials / build / size / use "
        "cases (use only what's reasonably implied — never invent specs). End with "
        "a short bullet list of 3-4 attributes (use <ul><li>). 80-130 words total. "
        "Plain neutral retail tone, no marketing fluff or superlatives.\n\n"
        f"Title: {title or 'Unknown product'}\n"
        f"Type: {product_type or 'unknown'}\n"
        f"Vendor: {vendor or 'unknown'}\n"
        f"Tags: {tag_text or 'none'}\n"
        f"Existing description (may be thin or empty): {existing_text or '(none)'}\n\n"
        "Return JSON: {\"body_html\": \"<p>…</p><ul><li>…</li></ul>\"}"
    )
    try:
        from openai import OpenAI
        client = OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You write neutral, factual ecommerce product descriptions in HTML."},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=400,
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        body = (parsed.get("body_html") or "").strip()
        if not body:
            return None
        # Strip any <script> tags as a basic safety net.
        body = _re.sub(r"<script.*?</script>", "", body, flags=_re.IGNORECASE | _re.DOTALL)
        return body[:4000]
    except Exception as exc:
        logger.warning("AI description generation failed: %s", exc)
        return None


@app.route("/integrations/shopify/descriptions/preview/<int:client_id>", methods=["POST"])
@login_required
def shopify_descriptions_preview(client_id):
    """Generate AI-rewritten descriptions for every product with a thin
    body. Stores the proposals in shop_meta so the user can review and
    approve before any write. Charges nothing on preview (no Shopify
    write yet) — the apply step charges credits."""
    from services.shopify_client import ShopifyAdminClient, ShopifyAPIError, scope_has
    from services.shopify_audit import _is_thin_description

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != current_user.id:
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    connection = (
        ShopifyConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).one_or_none()
    )
    if not connection:
        flash("No Shopify store connected.", "error")
        return redirect(url_for("client_detail", client_id=client_id))

    if not os.getenv("OPENAI_API_KEY"):
        flash(
            "Description rewrites need OPENAI_API_KEY configured on this server.",
            "error",
        )
        return redirect(url_for("shopify_products", client_id=client_id))

    admin = ShopifyAdminClient(connection.shop_domain, connection.access_token)
    try:
        products = admin.list_products(limit=50)
    except ShopifyAPIError as exc:
        flash(f"Could not load products: {exc}", "error")
        return redirect(url_for("shopify_products", client_id=client_id))

    proposals: List[Dict[str, Any]] = []
    for product in products:
        if not _is_thin_description(product):
            continue
        proposed = _generate_product_description_ai(product)
        if not proposed:
            continue
        proposals.append(
            {
                "product_id": product.get("id"),
                "title": product.get("title") or "Untitled product",
                "original_html": product.get("body_html") or "",
                "proposed_html": proposed,
            }
        )
        if len(proposals) >= 25:
            break  # cap a single preview pass to keep tokens bounded

    meta = dict(connection.shop_meta or {})
    meta["cached_description_proposals"] = proposals
    meta["cached_description_proposals_at"] = datetime.utcnow().isoformat()
    connection.shop_meta = meta
    flag_modified(connection, "shop_meta")
    db.session.commit()

    if not proposals:
        flash(
            "No thin product descriptions found, or AI generation failed for "
            "every candidate.",
            "info",
        )
    else:
        flash(
            f"Generated {len(proposals)} description{'s' if len(proposals) != 1 else ''}. "
            "Review the previews and apply the ones you want.",
            "success",
        )
    return redirect(url_for("shopify_products", client_id=client_id))


@app.route("/integrations/shopify/descriptions/apply/<int:client_id>", methods=["POST"])
@login_required
def shopify_descriptions_apply(client_id):
    """Push the user-approved description rewrites to Shopify."""
    from services.shopify_client import ShopifyAdminClient, ShopifyAPIError, scope_has

    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != current_user.id:
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    connection = (
        ShopifyConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).one_or_none()
    )
    if not connection:
        flash("No Shopify store connected.", "error")
        return redirect(url_for("client_detail", client_id=client_id))

    if not scope_has(connection.scope, "write_products"):
        flash(
            "This store was connected with read-only access. Reconnect to enable write-back.",
            "error",
        )
        return redirect(url_for("shopify_products", client_id=client_id))

    if not has_enough_credits_for(current_user, "description_rewrite_batch"):
        flash(
            f"You need {get_action_cost('description_rewrite_batch')} credits "
            "to apply description rewrites. Top up to continue.",
            "warning",
        )
        return redirect(url_for("shopify_products", client_id=client_id))

    proposals = (connection.shop_meta or {}).get("cached_description_proposals") or []
    selected_ids = {
        str(pid) for pid in request.form.getlist("apply_product_id") if str(pid).strip()
    }
    if not selected_ids:
        flash("No products selected.", "info")
        return redirect(url_for("shopify_products", client_id=client_id))

    admin = ShopifyAdminClient(connection.shop_domain, connection.access_token)
    patched = 0
    failed = 0
    for proposal in proposals:
        pid = proposal.get("product_id")
        if not pid or str(pid) not in selected_ids:
            continue
        body = proposal.get("proposed_html") or ""
        if not body:
            continue
        try:
            admin.update_product_description(pid, body)
            patched += 1
        except ShopifyAPIError as exc:
            logger.warning("Description PUT failed for product %s: %s", pid, exc)
            failed += 1

    if patched:
        spend_credits_for(
            current_user,
            "description_rewrite_batch",
            notes=f"Description rewrite: {patched} products",
        )
        # Clear proposals so the UI doesn't keep showing the same set.
        meta = dict(connection.shop_meta or {})
        meta.pop("cached_description_proposals", None)
        meta.pop("cached_description_proposals_at", None)
        connection.shop_meta = meta
        flag_modified(connection, "shop_meta")
        connection.last_synced_at = datetime.utcnow()
        # Refresh findings after the rewrite (descriptions just got fatter).
        try:
            refreshed = admin.list_products(limit=50)
            _refresh_shopify_findings(connection, refreshed)
        except ShopifyAPIError:
            pass
        db.session.commit()

    if patched and not failed:
        flash(f"Updated descriptions on {patched} product{'s' if patched != 1 else ''}.", "success")
    elif patched and failed:
        flash(
            f"Updated {patched}; {failed} update{'s' if failed != 1 else ''} failed.",
            "warning",
        )
    else:
        flash(f"Could not update any products ({failed} failures).", "error")
    return redirect(url_for("shopify_products", client_id=client_id))


@app.route("/integrations/shopify/disconnect/<int:client_id>", methods=["POST"])
@login_required
def shopify_disconnect(client_id):
    """Drop the stored access token for a workspace."""
    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != current_user.id:
        flash("Workspace not found.", "error")
        return redirect(url_for("index"))

    connection = (
        ShopifyConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).one_or_none()
    )
    if connection:
        db.session.delete(connection)
        db.session.commit()
        flash("Disconnected Shopify store.", "success")
    else:
        flash("No Shopify store to disconnect.", "info")
    return redirect(url_for("client_detail", client_id=client_id))


# ===========================================================================
# Module 2 + 3 connectors — foundation routes
# ===========================================================================
# Connect / disconnect for the five new integrations: BigCommerce + SHOPLINE
# (Module 3 — Ecommerce) and Wix + Framer + Squarespace (Module 2 — Website).
#
# Each follows the same pattern: a single hub page (`module_connectors`)
# renders all five connection forms; per-connector POST routes verify the
# credentials, persist them, and redirect back. No catalog sync, no
# audit-pipeline wiring, no write-back yet — those land per-connector when
# we extend each module past the foundation.
#
# All five connectors are untested against live APIs. The first end-to-end
# deploy needs at minimum a credential round-trip for each.


def _workspace_or_redirect(client_id: int):
    """Common guard for the connector routes — returns the workspace or
    redirects with a flash if it's not found / not the user's."""
    workspace = db.session.get(Client, client_id)
    if not workspace or workspace.user_id != current_user.id:
        flash("Workspace not found.", "error")
        return None
    return workspace


@app.route("/client/<int:client_id>/integrations/modules", methods=["GET"])
@login_required
def module_connectors(client_id):
    """Foundation hub for the five Module 2 + Module 3 connectors.

    Each section shows current connection status and a connect form.
    Disconnect + refresh actions live on this same page via POST routes.
    """
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))

    return render_template(
        "integrations/module_connectors.html",
        workspace=workspace,
        bigcommerce=BigCommerceConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).first(),
        shopline=ShoplineConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).first(),
        wix=WixConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).first(),
        framer=FramerConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).first(),
        squarespace=SquarespaceConnection.query.filter_by(
            user_id=current_user.id, client_id=client_id
        ).first(),
    )


# --- BigCommerce -----------------------------------------------------------

@app.route("/integrations/bigcommerce/<int:client_id>/connect", methods=["POST"])
@login_required
def bigcommerce_connect(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))

    from services.bigcommerce_client import (
        BigCommerceAPIError,
        BigCommerceConfigError,
        verify_connection,
    )

    store_hash = (request.form.get("store_hash") or "").strip()
    access_token = (request.form.get("access_token") or "").strip()

    try:
        meta = verify_connection(store_hash=store_hash, access_token=access_token)
    except (BigCommerceConfigError, BigCommerceAPIError) as exc:
        flash(f"BigCommerce connect failed: {exc}", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    existing = BigCommerceConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if existing:
        existing.store_hash = store_hash
        existing.access_token = access_token
        existing.store_meta = meta
        existing.last_synced_at = datetime.utcnow()
    else:
        db.session.add(
            BigCommerceConnection(
                user_id=current_user.id,
                client_id=client_id,
                store_hash=store_hash,
                access_token=access_token,
                store_meta=meta,
                last_synced_at=datetime.utcnow(),
            )
        )
    db.session.commit()
    flash("BigCommerce store connected.", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


@app.route("/integrations/bigcommerce/<int:client_id>/sync", methods=["POST"])
@login_required
def bigcommerce_sync(client_id):
    """Pull a page of products from BigCommerce, normalize them, and
    run the cross-platform ecommerce audit. Findings are stored in
    `store_meta.audit_summary` for display on the hub page."""
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))

    conn = BigCommerceConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if not conn:
        flash("Connect a BigCommerce store first.", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    from services.bigcommerce_client import (
        BigCommerceAPIError,
        BigCommerceClient,
    )
    from services.ecommerce_audit_adapter import normalize_bigcommerce_catalog
    from services.shopify_audit import summarize_catalog

    try:
        api = BigCommerceClient(
            store_hash=conn.store_hash, access_token=conn.access_token
        )
        raw = api.list_products(limit=100, page=1)
    except BigCommerceAPIError as exc:
        flash(f"Sync failed: {exc}", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    normalized = normalize_bigcommerce_catalog(raw)
    summary = summarize_catalog(normalized)

    meta = dict(conn.store_meta or {})
    meta["audit_summary"] = summary
    meta["last_sync_count"] = len(normalized)
    conn.store_meta = meta
    conn.last_synced_at = datetime.utcnow()
    db.session.commit()
    flash(f"Synced {len(normalized)} BigCommerce product(s).", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


@app.route("/integrations/bigcommerce/<int:client_id>/disconnect", methods=["POST"])
@login_required
def bigcommerce_disconnect(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))
    conn = BigCommerceConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if conn:
        db.session.delete(conn)
        db.session.commit()
        flash("Disconnected BigCommerce store.", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


# --- SHOPLINE -------------------------------------------------------------

@app.route("/integrations/shopline/<int:client_id>/connect", methods=["POST"])
@login_required
def shopline_connect(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))

    from services.shopline_client import (
        ShoplineAPIError,
        ShoplineConfigError,
        verify_connection,
    )

    store_handle = (request.form.get("store_handle") or "").strip()
    access_token = (request.form.get("access_token") or "").strip()

    try:
        meta = verify_connection(store_handle=store_handle, access_token=access_token)
    except (ShoplineConfigError, ShoplineAPIError) as exc:
        flash(f"SHOPLINE connect failed: {exc}", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    existing = ShoplineConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if existing:
        existing.store_handle = store_handle
        existing.access_token = access_token
        existing.shop_meta = meta
        existing.last_synced_at = datetime.utcnow()
    else:
        db.session.add(
            ShoplineConnection(
                user_id=current_user.id,
                client_id=client_id,
                store_handle=store_handle,
                access_token=access_token,
                shop_meta=meta,
                last_synced_at=datetime.utcnow(),
            )
        )
    db.session.commit()
    flash("SHOPLINE store connected.", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


@app.route("/integrations/shopline/<int:client_id>/sync", methods=["POST"])
@login_required
def shopline_sync(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))

    conn = ShoplineConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if not conn:
        flash("Connect a SHOPLINE store first.", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    from services.shopline_client import ShoplineAPIError, ShoplineClient
    from services.ecommerce_audit_adapter import normalize_shopline_catalog
    from services.shopify_audit import summarize_catalog

    try:
        api = ShoplineClient(
            store_handle=conn.store_handle, access_token=conn.access_token
        )
        raw = api.list_products(limit=100, page=1)
    except ShoplineAPIError as exc:
        flash(f"Sync failed: {exc}", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    normalized = normalize_shopline_catalog(raw)
    summary = summarize_catalog(normalized)

    meta = dict(conn.shop_meta or {})
    meta["audit_summary"] = summary
    meta["last_sync_count"] = len(normalized)
    conn.shop_meta = meta
    conn.last_synced_at = datetime.utcnow()
    db.session.commit()
    flash(f"Synced {len(normalized)} SHOPLINE product(s).", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


@app.route("/integrations/shopline/<int:client_id>/disconnect", methods=["POST"])
@login_required
def shopline_disconnect(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))
    conn = ShoplineConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if conn:
        db.session.delete(conn)
        db.session.commit()
        flash("Disconnected SHOPLINE store.", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


# --- Wix ------------------------------------------------------------------

@app.route("/integrations/wix/<int:client_id>/connect", methods=["POST"])
@login_required
def wix_connect(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))

    from services.wix_client import WixAPIError, WixConfigError, verify_connection

    site_id = (request.form.get("site_id") or "").strip()
    api_key = (request.form.get("api_key") or "").strip()

    try:
        meta = verify_connection(api_key=api_key, site_id=site_id)
    except (WixConfigError, WixAPIError) as exc:
        flash(f"Wix connect failed: {exc}", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    existing = WixConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if existing:
        existing.site_id = site_id
        existing.api_key = api_key
        existing.site_meta = meta
        existing.collections_cache = meta.get("collections") or []
        existing.last_synced_at = datetime.utcnow()
    else:
        db.session.add(
            WixConnection(
                user_id=current_user.id,
                client_id=client_id,
                site_id=site_id,
                api_key=api_key,
                site_meta=meta,
                collections_cache=meta.get("collections") or [],
                last_synced_at=datetime.utcnow(),
            )
        )
    db.session.commit()
    flash("Wix site connected.", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


@app.route("/integrations/wix/<int:client_id>/refresh-collections", methods=["POST"])
@login_required
def wix_refresh_collections(client_id):
    """Re-fetch the Wix CMS collection list and cache it on the
    connection. Lets the user pick a target collection in the publish
    UI without DarInsights calling the API on every page render."""
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))

    conn = WixConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if not conn:
        flash("Connect a Wix site first.", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    from services.wix_client import WixAPIError, WixClient

    try:
        api = WixClient(api_key=conn.api_key, site_id=conn.site_id)
        collections = api.list_collections()
    except WixAPIError as exc:
        flash(f"Wix collection refresh failed: {exc}", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    conn.collections_cache = collections
    conn.last_synced_at = datetime.utcnow()
    db.session.commit()
    flash(f"Found {len(collections)} Wix collection(s).", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


@app.route("/integrations/wix/<int:client_id>/disconnect", methods=["POST"])
@login_required
def wix_disconnect(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))
    conn = WixConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if conn:
        db.session.delete(conn)
        db.session.commit()
        flash("Disconnected Wix site.", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


# --- Framer ---------------------------------------------------------------

@app.route("/integrations/framer/<int:client_id>/connect", methods=["POST"])
@login_required
def framer_connect(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))

    from services.framer_client import (
        FramerAPIError,
        FramerConfigError,
        verify_connection,
    )

    project_id = (request.form.get("project_id") or "").strip()
    access_token = (request.form.get("access_token") or "").strip()

    try:
        meta = verify_connection(access_token=access_token, project_id=project_id)
    except (FramerConfigError, FramerAPIError) as exc:
        flash(f"Framer connect failed: {exc}", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    existing = FramerConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if existing:
        existing.project_id = project_id
        existing.access_token = access_token
        existing.project_meta = meta
        existing.last_synced_at = datetime.utcnow()
    else:
        db.session.add(
            FramerConnection(
                user_id=current_user.id,
                client_id=client_id,
                project_id=project_id,
                access_token=access_token,
                project_meta=meta,
                last_synced_at=datetime.utcnow(),
            )
        )
    db.session.commit()
    flash("Framer project connected.", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


@app.route("/integrations/framer/<int:client_id>/refresh-collections", methods=["POST"])
@login_required
def framer_refresh_collections(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))

    conn = FramerConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if not conn:
        flash("Connect a Framer project first.", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    from services.framer_client import FramerAPIError, FramerClient

    try:
        api = FramerClient(
            access_token=conn.access_token, project_id=conn.project_id
        )
        collections = api.list_collections()
    except FramerAPIError as exc:
        flash(f"Framer collection refresh failed: {exc}", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    conn.collections_cache = collections
    conn.last_synced_at = datetime.utcnow()
    db.session.commit()
    flash(f"Found {len(collections)} Framer collection(s).", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


@app.route("/integrations/framer/<int:client_id>/disconnect", methods=["POST"])
@login_required
def framer_disconnect(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))
    conn = FramerConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if conn:
        db.session.delete(conn)
        db.session.commit()
        flash("Disconnected Framer project.", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


# --- Squarespace ----------------------------------------------------------

@app.route("/integrations/squarespace/<int:client_id>/connect", methods=["POST"])
@login_required
def squarespace_connect(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))

    from services.squarespace_client import (
        SquarespaceAPIError,
        SquarespaceConfigError,
        verify_connection,
    )

    api_key = (request.form.get("api_key") or "").strip()

    try:
        meta = verify_connection(api_key=api_key)
    except (SquarespaceConfigError, SquarespaceAPIError) as exc:
        flash(f"Squarespace connect failed: {exc}", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    existing = SquarespaceConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if existing:
        existing.api_key = api_key
        existing.site_meta = meta
        existing.last_synced_at = datetime.utcnow()
    else:
        db.session.add(
            SquarespaceConnection(
                user_id=current_user.id,
                client_id=client_id,
                api_key=api_key,
                site_meta=meta,
                last_synced_at=datetime.utcnow(),
            )
        )
    db.session.commit()
    flash("Squarespace site connected.", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


@app.route("/integrations/squarespace/<int:client_id>/sync", methods=["POST"])
@login_required
def squarespace_sync(client_id):
    """Squarespace Commerce-only catalog sync — non-commerce sites
    return an empty list, which is a valid result."""
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))

    conn = SquarespaceConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if not conn:
        flash("Connect a Squarespace site first.", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    from services.squarespace_client import (
        SquarespaceAPIError,
        SquarespaceClient,
    )
    from services.ecommerce_audit_adapter import normalize_squarespace_catalog
    from services.shopify_audit import summarize_catalog

    try:
        api = SquarespaceClient(api_key=conn.api_key)
        body = api.list_products()
        raw = body.get("products") or []
    except SquarespaceAPIError as exc:
        flash(f"Sync failed: {exc}", "error")
        return redirect(url_for("module_connectors", client_id=client_id))

    normalized = normalize_squarespace_catalog(raw)
    summary = summarize_catalog(normalized)

    meta = dict(conn.site_meta or {})
    meta["audit_summary"] = summary
    meta["last_sync_count"] = len(normalized)
    conn.site_meta = meta
    conn.last_synced_at = datetime.utcnow()
    db.session.commit()
    if normalized:
        flash(f"Synced {len(normalized)} Squarespace product(s).", "success")
    else:
        flash(
            "Sync ran successfully — no commerce products found "
            "(Squarespace catalog reads only return data for Commerce sites).",
            "info",
        )
    return redirect(url_for("module_connectors", client_id=client_id))


@app.route("/integrations/squarespace/<int:client_id>/disconnect", methods=["POST"])
@login_required
def squarespace_disconnect(client_id):
    workspace = _workspace_or_redirect(client_id)
    if workspace is None:
        return redirect(url_for("index"))
    conn = SquarespaceConnection.query.filter_by(
        user_id=current_user.id, client_id=client_id
    ).first()
    if conn:
        db.session.delete(conn)
        db.session.commit()
        flash("Disconnected Squarespace site.", "success")
    return redirect(url_for("module_connectors", client_id=client_id))


@app.errorhandler(403)
def forbidden_error(error):
    """Render the branded 403 page instead of Flask's bare default."""
    return render_template("errors/403.html"), 403


@app.errorhandler(404)
def not_found_error(error):
    """Render the branded 404 page instead of Flask's bare default.

    Triggered by every `abort(404)` call (50+ sites — workspace lookup
    misses, audit not found, draft missing, etc.) plus any unmatched
    URL. Authenticated users keep their sidebar + topbar so they're
    not bounced out of the dashboard chrome on a stray bad link."""
    return render_template("errors/404.html"), 404


@app.errorhandler(429)
def rate_limited_error(error):
    """Render the branded 429 page (per-minute rate limit hit)."""
    return render_template("errors/429.html"), 429


@app.errorhandler(500)
def internal_server_error(error):
    """Render the branded 500 page. We rollback any pending DB session
    state so the user can retry without leaving stale transactions
    around. The full traceback is in app.logger; the user sees a
    friendly 'something broke' page with a Get help CTA."""
    try:
        db.session.rollback()
    except Exception:
        pass
    logger.exception("Unhandled 500 error: %s", error)
    return render_template("errors/500.html"), 500


if __name__ == "__main__":
    ensure_data_dirs()
    with app.app_context():
        db.create_all()
    print("Starting Flask app...")
    app.run(host="127.0.0.1", port=5001, debug=True)
