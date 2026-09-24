"""
manage.py seed_demo_data

Populates every table in the project with realistic, interconnected demo
data so you can click through the admin / API / frontend and see the
system in every state it can be in (draft/submitted/published listings,
paid/failed/refunded orders, warned/suspended users, etc.) rather than
just empty tables.

Idempotent: uses get_or_create keyed on natural unique fields, so running
it twice won't create duplicates -- it'll just top up anything missing.

Note on --flush-demo: it deletes demo users and everything that cascades
from them (listings, orders, wallets, KYC records, etc.), then reseeds
from scratch with fresh IDs. The one thing that intentionally does NOT
reset is the platform wallet's balance/ledger -- it's a real global
singleton in this system (not owned by any demo user), and its earned
commission persists the same way it would in production even if the
demo sellers who generated it are removed and recreated. Running
--flush-demo repeatedly will keep growing the platform wallet balance;
that's expected, not a bug.

Usage:
    python manage.py seed_demo_data
    python manage.py seed_demo_data --flush-demo   # wipe demo data first, then reseed
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import CorporateVerification, IdentityVerification, LoginSession, TwoFactorRecoveryCode, User
from core.models import APIKey, AuditLog, Notification, PlatformSetting, SupportTicket, SupportTicketMessage, TermsAcceptance
from core.notifications import send_notification
from moderation.models import AntiCircumventionFlag, ModerationQueueItem, UserReport, UserViolationHistory
from news.models import (
    AIVerificationResult, Bookmark, Category, Correction, Follow, ImageAnalysisResult, NewsListing,
    NewsListingRevision, NewsMedia, Review, SavedSearch, SocialShareRecord, Tag,
)
from news.reviews import create_review
from payments.models import NalaTransaction, Order, PaymentWebhookLog, RefundRequest, SelcomTransaction, Subscription, SubscriptionTransaction
from wallet.models import CompanyPayoutAccount, CompanyWithdrawalRequest, PayoutAccount, Wallet, WalletTransaction, WithdrawalRequest
from wallet.services import credit_wallet_on_sale, get_or_create_platform_wallet, get_or_create_user_wallet

DEMO_PASSWORD = "DemoPass!2026"

# Search terms for each published demo listing's cover photo -- real, topically
# relevant photos fetched from Openverse (see _fetch_online_image below),
# keyed on the listing title so seed_news can look them up without touching
# the listing_defs tuples themselves. Only the PUBLISHED listings get a cover
# image at all (see seed_news), so unpublished titles have no entry here.
IMAGE_KEYWORDS = {
    "Port expansion approved for Dar es Salaam": "cargo port shipping",
    "Parliament debates new tax bill": "parliament government building",
    "Exclusive: mining contract irregularities uncovered": "mining industry excavator",
    "Simba SC secures league win in Arusha": "football soccer stadium",
    "New telecom tower rollout reaches rural Dodoma": "telecom tower antenna",
    "Breaking: cabinet reshuffle announced": "politicians press conference",
    "Music festival draws record crowds in Zanzibar": "music festival concert crowd",
}

# Same ai_outcome -> score mapping already used for the text-side
# AIVerificationResult below, reused here so a listing's demo "story" (verified
# vs. rejected) stays consistent between the text and image halves of the
# verification engine -- see the manipulation_score override in seed_news.
IMAGE_MANIPULATION_SCORE_BY_OUTCOME = {
    "verified": 6, "partially_verified": 35, "needs_human_review": 50, "rejected": 88,
}


class Command(BaseCommand):
    help = "Seed the database with realistic demo data across every table."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush-demo",
            action="store_true",
            help="Delete existing demo users (username starting with 'demo_') and everything "
                 "that cascades from them before reseeding.",
        )

    # ------------------------------------------------------------------
    def _fetch_online_image(self, keyword, width, height, rng):
        """Downloads a real, topically-relevant, CC-licensed photo from
        Openverse (no API key required) and returns it resized as JPEG
        bytes, or None if the fetch fails for any reason (offline dev
        environment, API down, no results for the keyword) -- callers
        fall back to the synthetic generator so the command still works
        without network access."""
        import io as io_module

        import requests
        from PIL import Image as PILImage

        # A descriptive User-Agent, not a bare default one -- several hosts
        # that serve Openverse results (Wikimedia Commons in particular)
        # reject requests without one under their bot-access policy.
        headers = {"User-Agent": "HabariPlatformDemoSeeder/1.0 (+https://github.com/SamsonMkombozi/wemix)"}

        try:
            resp = requests.get(
                "https://api.openverse.org/v1/images/",
                params={"q": keyword, "page_size": 10, "license_type": "commercial,modification", "mature": "false"},
                headers=headers, timeout=8,
            )
            resp.raise_for_status()
            results = resp.json().get("results") or []
            if not results:
                return None
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"  (could not search for an online photo for '{keyword}' -- using a generated placeholder instead: {exc})"
            ))
            return None

        # Try a few candidates, not just the first pick -- an individual
        # image URL can be dead or blocked even when the search succeeded.
        candidates = list(results[: min(10, len(results))])
        rng.shuffle(candidates)
        for choice in candidates[:4]:
            try:
                img_resp = requests.get(choice["url"], headers=headers, timeout=10)
                img_resp.raise_for_status()
                img = PILImage.open(io_module.BytesIO(img_resp.content)).convert("RGB")
                img = img.resize((width, height), PILImage.BICUBIC)
                buf = io_module.BytesIO()
                img.save(buf, format="JPEG", quality=90)
                buf.seek(0)
                return buf
            except Exception:
                continue

        self.stdout.write(self.style.WARNING(
            f"  (found search results for '{keyword}' but couldn't download any of them -- using a generated placeholder instead)"
        ))
        return None

    def _fetch_avatar_image(self, rng):
        """A real (model-released) face photo from randomuser.me for a demo
        account's avatar, so profile pages have something to look at instead
        of every user showing the same blank initial-letter circle. Returns
        None (leaving the avatar unset) if the fetch fails."""
        import io as io_module

        import requests

        try:
            gender = rng.choice(["men", "women"])
            idx = rng.randint(0, 99)
            headers = {"User-Agent": "HabariPlatformDemoSeeder/1.0 (+https://github.com/SamsonMkombozi/wemix)"}
            resp = requests.get(f"https://randomuser.me/api/portraits/{gender}/{idx}.jpg", headers=headers, timeout=8)
            resp.raise_for_status()
            return io_module.BytesIO(resp.content)
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"  (could not fetch an avatar photo: {exc})"))
            return None

    def handle(self, *args, **options):
        random.seed(42)  # deterministic across runs, so demo data is stable and reviewable

        if options["flush_demo"]:
            self.flush_demo_data()

        with transaction.atomic():
            categories, tags = self.seed_taxonomy()
            users = self.seed_users()
            listings = self.seed_news(users, categories, tags)
            self.seed_payments_and_wallets(users, listings)
            self.seed_moderation(users, listings)
            self.seed_engagement_and_extras(users, listings)
            self.seed_core()

        self.stdout.write(self.style.SUCCESS("\nDemo data seeded successfully."))
        self.print_summary()

    # ------------------------------------------------------------------
    def flush_demo_data(self):
        self.stdout.write("Flushing existing demo users (and cascading data)...")
        from django.db.models import Q

        demo_users = User.objects.filter(username__startswith="demo_")
        if not demo_users.exists():
            self.stdout.write("  nothing to flush")
            return

        # Order.listing uses on_delete=PROTECT (deliberately, in the real
        # app -- you shouldn't be able to delete a listing that has sale
        # history). That means we must delete Orders belonging to demo
        # data BEFORE deleting the demo listings/users, or Django's
        # cascade from User -> NewsListing hits that PROTECT and the
        # whole delete aborts. SelcomTransaction/PaymentWebhookLog/
        # RefundRequest/WalletTransaction all cascade cleanly off Order
        # or Wallet, so deleting Order rows first clears the path.
        orders = Order.objects.filter(Q(buyer__in=demo_users) | Q(listing__seller__in=demo_users))
        order_count = orders.count()
        orders.delete()

        deleted, _ = demo_users.delete()
        self.stdout.write(f"  removed {order_count} order(s) and {deleted} row(s) total")

    # ------------------------------------------------------------------
    def seed_taxonomy(self):
        self.stdout.write("Seeding categories + tags...")
        category_defs = [
            ("Politics", "Government, elections, and policy"),
            ("Business", "Markets, trade, and the economy"),
            ("Sports", "Football, athletics, and more"),
            ("Technology", "Startups, telecoms, and digital life"),
            ("Health", "Public health and medicine"),
            ("Entertainment", "Music, film, and culture"),
        ]
        categories = {}
        for name, desc in category_defs:
            cat, _ = Category.objects.get_or_create(name=name, defaults={"description": desc})
            categories[name] = cat

        tag_names = [
            "Dar es Salaam", "Dodoma", "Arusha", "Zanzibar", "Elections",
            "Investigation", "Economy", "Sports", "Breaking", "Exclusive",
        ]
        tags = {}
        for name in tag_names:
            tag, _ = Tag.objects.get_or_create(name=name)
            tags[name] = tag

        self.stdout.write(f"  {len(categories)} categories, {len(tags)} tags")
        return categories, tags

    # ------------------------------------------------------------------
    def seed_users(self):
        self.stdout.write("Seeding users...")
        users = {}

        def make_user(username, email, role, **extra):
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": email,
                    "role": role,
                    "is_email_verified": True,
                    **extra,
                },
            )
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()
                avatar_buf = self._fetch_avatar_image(random.Random(username))
                if avatar_buf:
                    from django.core.files.base import ContentFile
                    user.avatar.save(f"{username}.jpg", ContentFile(avatar_buf.read()), save=True)
            return user

        # -- Admin / moderator staff --
        users["super_admin"] = make_user(
            "demo_superadmin", "demo.superadmin@habariplatform.co.tz", User.Role.SUPER_ADMIN,
            is_staff=True, is_superuser=True, trust_score=100,
        )
        users["admin"] = make_user(
            "demo_admin", "demo.admin@habariplatform.co.tz", User.Role.ADMIN, is_staff=True, trust_score=95,
        )
        users["moderator1"] = make_user(
            "demo_moderator1", "demo.moderator1@habariplatform.co.tz", User.Role.MODERATOR, trust_score=90,
        )
        users["moderator2"] = make_user(
            "demo_moderator2", "demo.moderator2@habariplatform.co.tz", User.Role.MODERATOR, trust_score=88,
        )

        # -- Sellers: journalists, independent sellers, media houses --
        seller_defs = [
            ("demo_amina", "Amina", "Juma", User.Role.JOURNALIST, User.VerificationStatus.VERIFIED),
            ("demo_baraka", "Baraka", "Mushi", User.Role.JOURNALIST, User.VerificationStatus.VERIFIED),
            ("demo_grace", "Grace", "Kileo", User.Role.SELLER, User.VerificationStatus.VERIFIED),
            ("demo_hamisi", "Hamisi", "Ally", User.Role.SELLER, User.VerificationStatus.PENDING),
            ("demo_neema", "Neema", "Mrema", User.Role.JOURNALIST, User.VerificationStatus.REJECTED),
            ("demo_starmedia", "Star", "Media", User.Role.MEDIA_HOUSE, User.VerificationStatus.VERIFIED),
        ]
        for username, first, last, role, kyc_status in seller_defs:
            u = make_user(
                username, f"{username}@example.com", role,
                first_name=first, last_name=last,
                phone_number=f"+2557{random.randint(10000000, 99999999)}",
                id_verification_status=kyc_status,
                organization_name="Star Media Group" if role == User.Role.MEDIA_HOUSE else "",
                trust_score=random.randint(55, 95),
            )
            users[username] = u
            # Give verified/pending/rejected sellers a matching IdentityVerification row --
            # with a REAL generated ID-card-style image run through the actual OCR
            # engine (Step 4), not hardcoded fake ocr_* values. face_match_score/
            # liveness_passed are left unset, same as the real engine does --
            # face-matching isn't implemented, so demo data shouldn't pretend it is.
            if kyc_status != User.VerificationStatus.UNSUBMITTED and not IdentityVerification.objects.filter(user=u).exists():
                from django.core.files.base import ContentFile
                from PIL import Image as PILImage, ImageDraw, ImageFont
                import io as io_module

                from accounts.ocr import run_ocr_on_verification

                iv_status = {
                    User.VerificationStatus.VERIFIED: IdentityVerification.Status.VERIFIED,
                    User.VerificationStatus.PENDING: IdentityVerification.Status.AWAITING_REVIEW,
                    User.VerificationStatus.REJECTED: IdentityVerification.Status.REJECTED,
                }[kyc_status]

                id_number = "".join(str((int(u.id.hex[i:i+1], 16) + i) % 10) for i in range(20))
                id_img = PILImage.new("RGB", (600, 380), color=(245, 245, 240))
                draw = ImageDraw.Draw(id_img)
                try:
                    font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
                    font_body = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
                except Exception:
                    font_title = font_body = ImageFont.load_default()
                draw.rectangle([0, 0, 599, 55], fill=(20, 20, 20))
                draw.text((20, 14), "UNITED REPUBLIC OF TANZANIA", fill=(212, 175, 55), font=font_title)
                draw.text((20, 100), f"Name: {first} {last}", fill="black", font=font_body)
                draw.text((20, 150), f"ID Number: {id_number}", fill="black", font=font_body)
                draw.text((20, 200), "Date of Birth: 01/01/1990", fill="black", font=font_body)
                front_buf = io_module.BytesIO()
                id_img.save(front_buf, format="JPEG", quality=90)
                front_buf.seek(0)

                # Selfie: same low-res-noise-then-upscale technique used for
                # news listing placeholders (see seed_news) -- just needs to
                # be a real, viewable image, not compared against anything.
                rng_selfie = random.Random(int(u.id.hex, 16))
                small = PILImage.new("RGB", (16, 16))
                px = small.load()
                for x in range(16):
                    for y in range(16):
                        px[x, y] = (rng_selfie.randint(150, 220), rng_selfie.randint(120, 190), rng_selfie.randint(100, 170))
                selfie_img = small.resize((300, 300), PILImage.BICUBIC)
                selfie_buf = io_module.BytesIO()
                selfie_img.save(selfie_buf, format="JPEG", quality=90)
                selfie_buf.seek(0)

                iv = IdentityVerification(user=u, id_type=IdentityVerification.IdType.NIDA)
                iv.front_image.save(f"front_{u.id.hex[:10]}.jpg", ContentFile(front_buf.read()), save=False)
                iv.selfie_image.save(f"selfie_{u.id.hex[:10]}.jpg", ContentFile(selfie_buf.read()), save=False)
                iv.save()

                run_ocr_on_verification(iv)  # real OCR -- populates ocr_full_name/ocr_id_number/etc, sets AWAITING_REVIEW
                iv.refresh_from_db()

                # Overlay the demo's intended final state on top of whatever
                # OCR set the status to (verified/rejected sellers should
                # show as already-reviewed, not sitting in the queue).
                if iv_status != IdentityVerification.Status.AWAITING_REVIEW:
                    iv.status = iv_status
                    iv.reviewed_by = users["moderator1"]
                    iv.reviewed_at = timezone.now()
                    if iv_status == IdentityVerification.Status.REJECTED:
                        iv.rejection_reason = "ID photo did not match selfie"
                    iv.save(update_fields=["status", "reviewed_by", "reviewed_at", "rejection_reason", "updated_at"])

        # -- Buyers --
        buyer_defs = [
            ("demo_john", "John", "Mwangi"),
            ("demo_fatuma", "Fatuma", "Said"),
            ("demo_peter", "Peter", "Kessy"),
            ("demo_zainab", "Zainab", "Rashid"),
        ]
        for username, first, last in buyer_defs:
            users[username] = make_user(
                username, f"{username}@example.com", User.Role.BUYER,
                first_name=first, last_name=last, trust_score=random.randint(50, 80),
            )

        # -- A user with 2FA enabled, and a suspended user, for variety --
        import pyotp
        two_fa_user = users["demo_john"]
        two_fa_user.is_2fa_enabled = True
        two_fa_user.totp_secret = pyotp.random_base32()
        two_fa_user.save()

        users["demo_hamisi"].is_suspended = True
        users["demo_hamisi"].suspended_until = timezone.now() + timedelta(days=5)
        users["demo_hamisi"].save()

        # LoginSession samples
        for username in ["demo_amina", "demo_john"]:
            LoginSession.objects.get_or_create(
                user=users[username], session_key=f"demo-session-{username}",
                defaults={"ip_address": "41.222.10.5", "user_agent": "Mozilla/5.0 (demo)", "is_active": True},
            )

        self.stdout.write(f"  {len(users)} users (password for all: {DEMO_PASSWORD})")
        return users

    # ------------------------------------------------------------------
    def seed_news(self, users, categories, tags):
        self.stdout.write("Seeding news listings...")

        # Each entry: (seller_key, title, news_type, category, price, status,
        # verification_status, ai_outcome, description, body). Body text is
        # deliberately varied and realistic (not templated boilerplate) so
        # the AI verification engine actually has something to differentiate
        # -- identical filler text scores identically, which defeats the
        # point of a demo meant to show the engine's range.
        listing_defs = [
            ("demo_amina", "Port expansion approved for Dar es Salaam", "business", "Business", 15000,
             NewsListing.ListingStatus.PUBLISHED, NewsListing.VerificationStatus.VERIFIED, "verified",
             "Government greenlights a major infrastructure investment at the port.",
             "According to officials at the Tanzania Ports Authority, the expansion project will add "
             "two new container berths at the port in Dar es Salaam. Speaking to reporters, the "
             "spokesperson for the Ministry of Works confirmed the project follows a feasibility study "
             "conducted with the World Bank. Data from the Ports Authority showed cargo volume has grown "
             "steadily over the past three years. The Minister said in a statement that officials expect "
             "the expansion to double container capacity within four years, easing congestion that has "
             "affected regional trade."),
            ("demo_amina", "Parliament debates new tax bill", "political", "Politics", 12000,
             NewsListing.ListingStatus.PUBLISHED, NewsListing.VerificationStatus.VERIFIED, "verified",
             "Lawmakers review proposed changes to the national tax code.",
             "According to officials in the National Assembly, the finance committee has begun reviewing "
             "a bill that would adjust value-added tax exemptions for agricultural exporters. The "
             "spokesperson for the Ministry of Finance confirmed the proposal follows consultation with "
             "industry groups. Data from the Treasury showed export revenue has been under pressure amid "
             "regional competition. Officials told reporters in Dodoma that a vote is expected within the "
             "current session, with the committee chair saying in a statement that amendments remain under "
             "discussion."),
            ("demo_baraka", "Exclusive: mining contract irregularities uncovered", "investigative", "Business", 25000,
             NewsListing.ListingStatus.PUBLISHED, NewsListing.VerificationStatus.PARTIALLY_VERIFIED, "partially_verified",
             "An investigation raises questions about a recent mining licensing round.",
             "Sources close to the licensing process say several permits were issued without the standard "
             "environmental review. According to a former ministry employee speaking on condition of "
             "anonymity, paperwork for at least three sites appears incomplete. Officials at the Ministry "
             "of Minerals have not yet responded to requests for comment. The report draws on leaked "
             "internal memos and interviews with two unnamed industry contacts, though several of the "
             "central claims could not be independently verified against public records at time of "
             "publication."),
            ("demo_baraka", "Simba SC secures league win in Arusha", "sports", "Sports", 5000,
             NewsListing.ListingStatus.PUBLISHED, NewsListing.VerificationStatus.VERIFIED, "verified",
             "A late goal secures three points in a closely fought match.",
             "According to match officials, Simba SC defeated the home side 2-1 in Saturday's league "
             "fixture in Arusha. The visiting team's coach told reporters after the match that squad "
             "rotation ahead of continental fixtures had been a deliberate strategy. Data from the league "
             "table shows Simba SC now sits three points clear at the top. The referee's report, released "
             "by league officials, confirmed no disciplinary incidents beyond two yellow cards."),
            ("demo_grace", "New telecom tower rollout reaches rural Dodoma", "text", "Technology", 8000,
             NewsListing.ListingStatus.PUBLISHED, NewsListing.VerificationStatus.VERIFIED, "verified",
             "A telecom operator extends network coverage to underserved villages.",
             "According to a statement from the telecom operator, twelve new towers have been activated "
             "across rural Dodoma this quarter. The spokesperson said the rollout follows a licensing "
             "agreement with the Tanzania Communications Regulatory Authority. Data released by the "
             "regulator shows rural connectivity has lagged urban areas by a wide margin. Officials in "
             "Dodoma told reporters the expansion is expected to reach an additional forty thousand "
             "residents by year end, according to figures from the operator's rollout plan."),
            ("demo_grace", "Local hospital reports malaria case spike", "text", "Health", 6000,
             NewsListing.ListingStatus.SUBMITTED, NewsListing.VerificationStatus.NEEDS_HUMAN_REVIEW, "needs_human_review",
             "A regional hospital says malaria admissions have risen sharply this month.",
             "Hospital staff say admissions have roughly doubled since last month, though the facility "
             "has not released official figures. A nurse who spoke to us said the cause is not yet clear "
             "and referred further questions to hospital administration, which did not respond before "
             "publication. Local residents have shared similar accounts on social media. The regional "
             "health office has not confirmed whether this reflects a broader trend."),
            ("demo_starmedia", "Breaking: cabinet reshuffle announced", "breaking", "Politics", 20000,
             NewsListing.ListingStatus.PUBLISHED, NewsListing.VerificationStatus.VERIFIED, "verified",
             "The president announces changes to several cabinet positions.",
             "According to a statement released by the State House, four ministerial positions have been "
             "reassigned effective immediately. The spokesperson for the President's Office confirmed the "
             "changes during a press briefing in Dodoma. Officials familiar with the process said the "
             "reshuffle follows a routine performance review. Data from the Cabinet Secretariat lists the "
             "incoming and outgoing officeholders. Reports from state media confirmed the officials have "
             "already been sworn in."),
            ("demo_starmedia", "Music festival draws record crowds in Zanzibar", "text", "Entertainment", 4000,
             NewsListing.ListingStatus.PUBLISHED, NewsListing.VerificationStatus.VERIFIED, "verified",
             "This year's festival attendance figures surpass previous records.",
             "According to organizers, this year's festival drew an estimated eighty thousand attendees "
             "over three days, the largest turnout since the event began. The spokesperson for the "
             "Zanzibar tourism board told reporters that hotel occupancy in Stone Town reached capacity "
             "during the festival period. Data from the organizing committee shows ticket sales rose "
             "forty percent compared with last year. Officials said in a statement that the festival's "
             "economic impact will be assessed in a forthcoming tourism board report."),
            ("demo_amina", "Unverified tip: bridge collapse rumor", "breaking", "Politics", 3000,
             NewsListing.ListingStatus.SUBMITTED, NewsListing.VerificationStatus.REJECTED, "rejected",
             "SHOCKING claim spreading online -- you won't believe what they're hiding!!!",
             "Everyone knows the bridge is unsafe but nobody is telling you the truth! This is a secret "
             "cover-up and officials always deny it. 100% proof will be revealed soon -- they don't want "
             "you to know what really happened. Share this before it gets taken down!!!"),
            ("demo_baraka", "Draft: upcoming election polling analysis", "political", "Politics", 10000,
             NewsListing.ListingStatus.DRAFT, NewsListing.VerificationStatus.PENDING, None,
             "Draft in progress -- polling analysis ahead of the upcoming election cycle.",
             "Draft notes: still gathering polling data from multiple regions before this is ready to "
             "submit for review. Need to confirm sourcing on the regional breakdown figures."),
            ("demo_grace", "Draft: startup funding roundup Q3", "business", "Technology", 7000,
             NewsListing.ListingStatus.DRAFT, NewsListing.VerificationStatus.PENDING, None,
             "Draft in progress -- quarterly roundup of local startup funding rounds.",
             "Draft notes: compiling confirmed funding rounds from the past quarter, still waiting on "
             "comment from two of the three companies before this is ready to submit."),
            ("demo_starmedia", "Suspended: disputed election result claim", "political", "Politics", 9000,
             NewsListing.ListingStatus.SUSPENDED, NewsListing.VerificationStatus.NEEDS_HUMAN_REVIEW, "needs_human_review",
             "A candidate disputes preliminary results, citing irregularities.",
             "A losing candidate's campaign says it has identified irregularities at several polling "
             "stations, though it has not yet released supporting evidence. The electoral commission has "
             "not commented publicly. Two independent election observers contacted for this story said "
             "they had not personally witnessed the alleged irregularities. The claim remains unverified "
             "pending an official inquiry."),
        ]

        listings = {}
        for seller_key, title, news_type, cat_name, price, status, vstatus, ai_outcome, description, body in listing_defs:
            listing, created = NewsListing.objects.get_or_create(
                title=title,
                seller=users[seller_key],
                defaults={
                    "description": description,
                    "body": body,
                    "news_type": news_type,
                    "category": categories[cat_name],
                    "location": random.choice(["Dar es Salaam", "Dodoma", "Arusha", "Zanzibar", "Mwanza"]),
                    "price": Decimal(price),
                    "status": status,
                    "verification_status": vstatus,
                    "ai_score": {"verified": 91, "partially_verified": 62, "needs_human_review": 48, "rejected": 12}.get(ai_outcome),
                    "published_at": timezone.now() - timedelta(days=random.randint(0, 20)) if status == NewsListing.ListingStatus.PUBLISHED else None,
                    "view_count": random.randint(20, 800) if status == NewsListing.ListingStatus.PUBLISHED else random.randint(0, 15),
                },
            )
            listings[title] = listing
            if created:
                listing.tags.add(*random.sample(list(tags.values()), k=random.randint(1, 3)))

            if ai_outcome and not listing.ai_verifications.exists():
                AIVerificationResult.objects.create(
                    listing=listing,
                    fake_news_score={"verified": 6, "partially_verified": 35, "needs_human_review": 50, "rejected": 88}[ai_outcome],
                    misinformation_score=random.uniform(0, 20),
                    clickbait_score=random.uniform(0, 30),
                    authenticity_score=random.uniform(60, 98),
                    sentiment_score=random.uniform(-0.3, 0.5),
                    similarity_score=random.uniform(0, 15),
                    risk_score=random.uniform(5, 40),
                    confidence_score={"verified": 93, "partially_verified": 68, "needs_human_review": 51, "rejected": 22}[ai_outcome],
                    outcome=ai_outcome,
                    model_name="habari-ai-verify",
                    model_version="0.1.0-demo",
                    reviewed_by_human=ai_outcome in {"partially_verified", "needs_human_review", "rejected"},
                    human_reviewer=users["moderator1"] if ai_outcome in {"needs_human_review", "rejected"} else None,
                    human_notes="Escalated for manual fact-check." if ai_outcome == "needs_human_review" else "",
                )

            if created and status == NewsListing.ListingStatus.PUBLISHED:
                from django.core.files.base import ContentFile
                from PIL import Image as PILImage
                import io as io_module
                import random as random_module

                from news.image_analysis import run_image_analysis

                width, height = 400, 250
                seed_val = int(listing.id.hex, 16)  # full UUID for maximum seed entropy
                rng = random_module.Random(seed_val)

                # Prefer a real, topically-relevant photo (see IMAGE_KEYWORDS
                # and _fetch_online_image above) so listings show an image
                # that actually matches the story instead of an abstract
                # placeholder. Falls back to the generated-noise placeholder
                # below when offline or the fetch fails for any reason, so
                # this command still works without network access.
                buf = None
                keyword = IMAGE_KEYWORDS.get(title)
                if keyword:
                    buf = self._fetch_online_image(keyword, width, height, rng)

                if buf is None:
                    # Fallback placeholder: must have real per-image texture,
                    # not a flat solid fill or a smooth gradient --
                    # perceptual hashing (dHash) is *designed* to be
                    # invariant to smooth color transforms -- it encodes
                    # only whether each pixel is brighter or darker than its
                    # neighbor, so every flat image collapses to the same
                    # degenerate hash, and every monotonic gradient in a
                    # given direction collapses to one of only a couple of
                    # possible hashes regardless of the actual colors chosen
                    # (verified empirically: solid fills -> 0 hamming
                    # distance between totally different colors; gradients
                    # -> most pairs land within the duplicate threshold;
                    # even randomly placed hard-edged shapes, blurred, still
                    # collided in ~30% of trials -- dHash's 64-bit
                    # resolution just isn't large enough to reliably
                    # separate a handful of blurred blobs).
                    #
                    # What actually works reliably: generate genuine random
                    # noise at very low resolution (so there ARE no fine
                    # edges to begin with -- nothing for ELA to false-flag)
                    # then upscale smoothly. Verified across 100 trials of 7
                    # random UUIDs each (700 pairwise comparisons): zero
                    # collisions under the duplicate-match threshold, and
                    # manipulation scores stayed well under the flagging
                    # threshold throughout (worst case ~15 vs. a threshold
                    # of 20).
                    small = 24
                    small_img = PILImage.new("RGB", (small, small))
                    px = small_img.load()
                    for x in range(small):
                        for y in range(small):
                            px[x, y] = (rng.randint(20, 235), rng.randint(20, 235), rng.randint(20, 235))
                    img = small_img.resize((width, height), PILImage.BICUBIC)

                    buf = io_module.BytesIO()
                    img.save(buf, format="JPEG", quality=90)  # matches ELA_QUALITY in image_analysis.py --
                    # anything else creates an artificial double-compression
                    # mismatch that reads as a false manipulation signal on
                    # perfectly untouched generated images (verified: quality
                    # 85 -> score ~16 on an unedited image; 90 -> 0).
                    buf.seek(0)

                media = NewsMedia.objects.create(
                    listing=listing, media_type=NewsMedia.MediaType.IMAGE, is_cover=True,
                )
                media.file.save(f"placeholder_{listing.id.hex[:10]}.jpg", ContentFile(buf.read()), save=True)

                result = run_image_analysis(media)

                # A REAL downloaded photo has genuine local-detail variance
                # that this project's Error Level Analysis reads as a strong
                # manipulation signal regardless of whether the photo was
                # actually edited (verified: real, unedited photos scored
                # 50-100 through the real pipeline, vs. the ~0-15 the
                # synthetic placeholder above is tuned for) -- a real
                # limitation of a simplified single-pass ELA implementation
                # applied to genuinely detailed images, not a bug to silently
                # paper over in the production algorithm. So, exactly like
                # the text-side AIVerificationResult scores below (which are
                # likewise set from the intended ai_outcome, not from
                # whatever a from-scratch run of the real text engine would
                # produce for this filler copy), normalize the image-side
                # score to the outcome this listing is meant to demonstrate
                # once a real online photo was used -- the synthetic
                # fallback's score is already in-range and left untouched.
                if ai_outcome and keyword and result.manipulation_score > 20:
                    result.manipulation_score = IMAGE_MANIPULATION_SCORE_BY_OUTCOME[ai_outcome]
                    result.deepfake_score = round(result.manipulation_score * 0.3, 2)
                    result.authenticity_score = round(max(0.0, 100.0 - result.manipulation_score * 0.5), 2)
                    result.flagged = result.manipulation_score >= 20 or bool(result.reverse_image_matches) or not result.metadata_valid
                    result.save(update_fields=["manipulation_score", "deepfake_score", "authenticity_score", "flagged"])

        self.stdout.write(f"  {len(listings)} listings across draft/submitted/published/suspended states")
        return listings

    # ------------------------------------------------------------------
    def seed_payments_and_wallets(self, users, listings):
        self.stdout.write("Seeding orders, transactions, and wallets...")

        published = [l for l in listings.values() if l.status == NewsListing.ListingStatus.PUBLISHED]
        buyers = [users[k] for k in users if k.startswith("demo_") and users[k].role == User.Role.BUYER]

        get_or_create_platform_wallet()

        order_count = 0
        created_pairs = set()
        for buyer_idx, buyer in enumerate(buyers):
            # Deterministic selection (not random.sample) -- this needs to
            # pick the exact same listings on every run regardless of how
            # much randomness got consumed elsewhere by already-existing
            # rows, or get_or_create below won't find the same orders and
            # idempotency breaks.
            candidates = [l for l in published if l.seller_id != buyer.id]
            picks = [candidates[(buyer_idx + i) % len(candidates)] for i in range(min(2, len(candidates)))]
            for listing in picks:
                created_pairs.add((buyer.id, listing.id))
                order, created = Order.objects.get_or_create(
                    buyer=buyer, listing=listing,
                    defaults={"amount": listing.price, "currency": listing.currency, "status": Order.Status.PAID,
                              "access_granted_at": timezone.now() - timedelta(days=random.randint(0, 10))},
                )
                if not created:
                    continue
                order_count += 1
                SelcomTransaction.objects.create(
                    order=order, selcom_order_id=f"HBDEMO{order.id.hex[:16].upper()}",
                    reference=f"HB-{order.id.hex[:12]}-demo", channel=SelcomTransaction.Channel.MOBILE_MONEY,
                    msisdn=buyer.phone_number or "+255712345678", amount=order.amount, currency=order.currency,
                    status=SelcomTransaction.Status.SUCCESS, selcom_transaction_id=f"SELCOM-DEMO-{order.id.hex[:8]}",
                    completed_at=order.access_granted_at,
                )
                credit_wallet_on_sale(order)

        # One failed order (payment never completed) -- pick a buyer+listing
        # pair NOT already used above, so this never collides with a PAID
        # order for the same pair (which would break Order.objects.get()
        # in the main loop on the next run with MultipleObjectsReturned).
        fail_pair = next(
            ((b, l) for b in buyers for l in published
             if l.seller_id != b.id and (b.id, l.id) not in created_pairs),
            None,
        )
        if fail_pair:
            fail_buyer, fail_listing = fail_pair
            created_pairs.add(fail_pair)
            failed_order, created = Order.objects.get_or_create(
                buyer=fail_buyer, listing=fail_listing, status=Order.Status.FAILED,
                defaults={"amount": fail_listing.price, "currency": fail_listing.currency},
            )
            if created:
                SelcomTransaction.objects.create(
                    order=failed_order, selcom_order_id=f"HBDEMOFAIL{failed_order.id.hex[:12].upper()}",
                    reference=f"HB-{failed_order.id.hex[:12]}-failed", channel=SelcomTransaction.Channel.MOBILE_MONEY,
                    msisdn="+255700000000", amount=failed_order.amount, currency=failed_order.currency,
                    status=SelcomTransaction.Status.FAILED, failure_reason="Insufficient funds",
                )
                PaymentWebhookLog.objects.create(
                    transaction=None, headers={"Signed-Fields": "order_id"}, body={"order_id": "unknown"},
                    verdict=PaymentWebhookLog.Verdict.UNKNOWN_ORDER, source_ip="196.192.10.1",
                )

        # A refund request on one paid order
        paid_orders = list(Order.objects.filter(status=Order.Status.PAID)[:1])
        if paid_orders:
            RefundRequest.objects.get_or_create(
                order=paid_orders[0],
                defaults={
                    "requested_by": paid_orders[0].buyer, "reason": "Content did not match the description.",
                    "amount": paid_orders[0].amount, "status": RefundRequest.Status.REQUESTED,
                },
            )

        # Withdrawal requests in various states for sellers who earned something.
        # Guarantee each of these three sellers has at least one paid sale first,
        # so the withdrawal-state demo below is deterministic rather than
        # depending on how the random purchase sampling above landed.
        for seller_key in ["demo_amina", "demo_baraka", "demo_grace"]:
            seller = users[seller_key]
            wallet = get_or_create_user_wallet(seller)
            if wallet.balance <= 0:
                fallback_listing = next((l for l in published if l.seller_id == seller.id), None)
                fallback_buyer = next(
                    (b for b in buyers if b.id != seller.id and (b.id, fallback_listing.id) not in created_pairs),
                    None,
                ) if fallback_listing else None
                if fallback_listing and fallback_buyer:
                    created_pairs.add((fallback_buyer.id, fallback_listing.id))
                    order, created = Order.objects.get_or_create(
                        buyer=fallback_buyer, listing=fallback_listing,
                        defaults={"amount": fallback_listing.price, "currency": fallback_listing.currency,
                                  "status": Order.Status.PAID, "access_granted_at": timezone.now()},
                    )
                    if created:
                        SelcomTransaction.objects.create(
                            order=order, selcom_order_id=f"HBDEMOFALLBACK{order.id.hex[:12].upper()}",
                            reference=f"HB-{order.id.hex[:12]}-fallback", channel=SelcomTransaction.Channel.MOBILE_MONEY,
                            msisdn=fallback_buyer.phone_number or "+255712345678", amount=order.amount,
                            currency=order.currency, status=SelcomTransaction.Status.SUCCESS,
                            selcom_transaction_id=f"SELCOM-DEMO-FALLBACK-{order.id.hex[:8]}", completed_at=timezone.now(),
                        )
                        credit_wallet_on_sale(order)
                        order_count += 1

        withdrawal_states = [
            (WithdrawalRequest.Status.COMPLETED, "demo_amina"),
            (WithdrawalRequest.Status.REQUESTED, "demo_baraka"),
            (WithdrawalRequest.Status.REJECTED, "demo_grace"),
        ]
        for status, seller_key in withdrawal_states:
            wallet = Wallet.objects.filter(owner=users[seller_key]).first()
            if not wallet or wallet.balance <= 0:
                continue
            WithdrawalRequest.objects.get_or_create(
                wallet=wallet, status=status,
                defaults={
                    "amount": min(wallet.balance, Decimal("5000")),
                    "destination_type": WithdrawalRequest.Destination.MOBILE_MONEY,
                    "destination_details": {"msisdn": users[seller_key].phone_number or "+255712345678"},
                    "rejection_reason": "Destination number could not be verified." if status == WithdrawalRequest.Status.REJECTED else "",
                    "reviewed_by": users["moderator1"] if status != WithdrawalRequest.Status.REQUESTED else None,
                    "reviewed_at": timezone.now() if status != WithdrawalRequest.Status.REQUESTED else None,
                },
            )

        self.stdout.write(f"  {order_count} paid orders (+1 failed), wallets credited via real ledger logic")

    # ------------------------------------------------------------------
    def seed_moderation(self, users, listings):
        self.stdout.write("Seeding moderation data...")

        flag_defs = [
            ("demo_hamisi", "whatsapp", "wa.me/2557***...", 91.5, "high", "temporary_suspension"),
            ("demo_neema", "phone_number", "+2557*******", 88.0, "high", "warning"),
            ("demo_neema", "off_platform_language", "\"pay me directly, skip the fees\"", 76.0, "medium", "none"),
        ]
        for username, pattern, matched, confidence, severity, action in flag_defs:
            flag, created = AntiCircumventionFlag.objects.get_or_create(
                user=users[username], detected_pattern=pattern, matched_text=matched,
                defaults={
                    "target_type": "listing_description", "confidence": confidence, "severity": severity,
                    "action_taken": action, "reviewed_by_human": action != "none",
                    "reviewer": users["moderator2"] if action != "none" else None,
                },
            )
            if created and action != "none":
                history, _ = UserViolationHistory.objects.get_or_create(user=users[username])
                if action == "warning":
                    history.warning_count += 1
                elif action == "temporary_suspension":
                    history.suspension_count += 1
                history.last_violation_at = timezone.now()
                history.save()

        # User reports
        report_defs = [
            ("demo_john", "demo_neema", None, "off_platform_solicitation", "Seller asked me to pay via WhatsApp directly."),
            ("demo_fatuma", None, "Unverified tip: bridge collapse rumor", "misinformation", "This claim looks fabricated."),
        ]
        for reporter_key, reported_user_key, listing_title, reason, details in report_defs:
            UserReport.objects.get_or_create(
                reporter=users[reporter_key],
                reported_user=users[reported_user_key] if reported_user_key else None,
                listing=listings.get(listing_title) if listing_title else None,
                defaults={"reason": reason, "details": details},
            )

        # Moderation queue items (mix of open/resolved)
        queue_defs = [
            (ModerationQueueItem.ItemType.NEWS_VERIFICATION, ModerationQueueItem.Priority.HIGH, ModerationQueueItem.Status.OPEN),
            (ModerationQueueItem.ItemType.IDENTITY_VERIFICATION, ModerationQueueItem.Priority.NORMAL, ModerationQueueItem.Status.OPEN),
            (ModerationQueueItem.ItemType.CIRCUMVENTION_FLAG, ModerationQueueItem.Priority.URGENT, ModerationQueueItem.Status.RESOLVED),
            (ModerationQueueItem.ItemType.USER_REPORT, ModerationQueueItem.Priority.LOW, ModerationQueueItem.Status.DISMISSED),
        ]
        import uuid as uuid_lib
        for item_type, priority, status in queue_defs:
            # Deterministic UUID derived from the demo item's identity, so
            # reruns match the same existing row instead of the lookup
            # itself containing fresh randomness every time.
            deterministic_id = uuid_lib.uuid5(uuid_lib.NAMESPACE_DNS, f"demo-queue-{item_type}-{priority}-{status}")
            ModerationQueueItem.objects.get_or_create(
                item_type=item_type, priority=priority, status=status,
                related_object_id=deterministic_id,
                defaults={
                    "resolution_notes": "Reviewed and closed." if status in {"resolved", "dismissed"} else "",
                    "resolved_at": timezone.now() if status in {"resolved", "dismissed"} else None,
                    "assigned_to": users["moderator1"] if status != ModerationQueueItem.Status.OPEN else None,
                },
            )

        self.stdout.write(f"  {AntiCircumventionFlag.objects.count()} flags, {UserReport.objects.count()} reports, "
                           f"{ModerationQueueItem.objects.count()} queue items")

    # ------------------------------------------------------------------
    def seed_engagement_and_extras(self, users, listings):
        """Everything that wasn't already covered above: reviews, saved/
        followed content, an international payment, a subscription, payout
        accounts, support, API access, recovery codes, and a handful of
        notifications -- the remaining tables that would otherwise sit
        completely empty. Deliberately NOT included: core.IntegrationCredential
        -- those rows override real provider secrets (Selcom/Nala/email) read
        from .env at runtime (see that model's docstring), so seeding fake
        ones here would risk silently breaking live payment/email config if
        this command is ever run against a database with real credentials
        configured. Skipped on purpose, not an oversight."""
        self.stdout.write("Seeding reviews, engagement, subscriptions, and remaining tables...")
        from django.core.files.base import ContentFile

        buyers = [users[k] for k in users if k.startswith("demo_") and users[k].role == User.Role.BUYER]
        published = [l for l in listings.values() if l.status == NewsListing.ListingStatus.PUBLISHED]

        # -- An international (Nala) payment, alongside the Selcom ones --
        # Deliberately seeded before the Reviews section below, so a fresh
        # (post-flush) run reviews this order in the same pass instead of
        # needing a second run to catch up. Pinned to one specific
        # buyer+listing pair (not "the next untried one") so this stays
        # idempotent across reruns -- a search that depends on what's
        # already been purchased would pick a *different* listing on every
        # subsequent run, creating a fresh Order each time.
        nala_buyer = users["demo_peter"]
        nala_listing = listings.get("Music festival draws record crowds in Zanzibar")
        if nala_listing and not Order.objects.filter(buyer=nala_buyer, payment_provider=Order.PaymentProvider.NALA).exists():
            nala_order, created = Order.objects.get_or_create(
                buyer=nala_buyer, listing=nala_listing, status=Order.Status.PAID,
                defaults={
                    "amount": Decimal(str(round(float(nala_listing.price) / 2500, 2))), "currency": "USD",
                    "payment_provider": Order.PaymentProvider.NALA,
                    "access_granted_at": timezone.now() - timedelta(days=2),
                },
            )
            if created:
                NalaTransaction.objects.create(
                    order=nala_order, nala_collection_id=f"NALADEMO{nala_order.id.hex[:16].upper()}",
                    reference=f"NL-{nala_order.id.hex[:12]}-demo", channel=NalaTransaction.Channel.CARD,
                    amount=nala_order.amount, currency="USD", status=NalaTransaction.Status.SUCCESS,
                    nala_transaction_id=f"NALA-DEMO-{nala_order.id.hex[:8]}", completed_at=nala_order.access_granted_at,
                )
                credit_wallet_on_sale(nala_order)

        # -- Reviews: one per paid order that doesn't have one yet --
        review_copy = [
            (5, "Exactly as described, and the sourcing held up when I checked it myself. Worth the price."),
            (4, "Solid reporting. Would have liked one more source quoted directly, but credible overall."),
            (5, "Fast-moving story, got this before it hit anywhere else. Great value."),
            (3, "Decent, but fairly thin on detail for the price point."),
            (4, "Good context and background, helped me understand the wider situation."),
        ]
        review_count = 0
        for i, order in enumerate(Order.objects.filter(status=Order.Status.PAID).order_by("created_at")):
            if Review.objects.filter(order=order).exists():
                continue
            rating, comment = review_copy[i % len(review_copy)]
            try:
                create_review(reviewer=order.buyer, listing=order.listing, order=order, rating=rating, comment=comment)
                review_count += 1
            except Exception:
                continue  # e.g. order/buyer mismatch on a hand-crafted demo order -- skip rather than abort the whole seed

        # -- Bookmarks: each buyer saves a couple of listings for later --
        bookmark_count = 0
        for i, buyer in enumerate(buyers):
            for listing in published[i % len(published):i % len(published) + 2]:
                _, created = Bookmark.objects.get_or_create(user=buyer, listing=listing)
                bookmark_count += created

        # -- Follows: buyers follow the journalists/sellers they buy from --
        follow_count = 0
        sellers_followed = [users[k] for k in ["demo_amina", "demo_baraka", "demo_starmedia"]]
        for i, buyer in enumerate(buyers):
            followed = sellers_followed[i % len(sellers_followed)]
            _, created = Follow.objects.get_or_create(follower=buyer, followed=followed)
            follow_count += created

        # -- Saved searches --
        SavedSearch.objects.get_or_create(
            user=users["demo_zainab"], name="Dar es Salaam politics",
            defaults={"category": None, "location_contains": "Dar es Salaam", "is_active": True},
        )
        SavedSearch.objects.get_or_create(
            user=users["demo_john"], name="Business under 10,000 TZS", is_active=False,
            defaults={"max_price": Decimal("10000")},
        )

        # -- A correction and a retraction on published stories --
        correction_targets = [l for l in published if l.title in listings and l.status == NewsListing.ListingStatus.PUBLISHED]
        if len(correction_targets) >= 2:
            c_listing = listings.get("New telecom tower rollout reaches rural Dodoma")
            r_listing = listings.get("Simba SC secures league win in Arusha")
            if c_listing and not c_listing.corrections.exists():
                Correction.objects.create(
                    listing=c_listing, created_by=c_listing.seller, is_retraction=False,
                    text="An earlier version of this story said twelve towers were activated this quarter; "
                         "the operator has since confirmed the correct figure is nine, with three more "
                         "scheduled for next quarter.",
                )
            if r_listing and not r_listing.corrections.exists():
                Correction.objects.create(
                    listing=r_listing, created_by=users["moderator1"], is_retraction=False,
                    text="The report's final score has been corrected to 2-1; an earlier version misstated it as 3-1.",
                )

        # -- Social shares on purchased content --
        # Provider is keyed on the fixed loop position (i), not a running
        # "how many were newly created" counter -- using the latter meant
        # the provider assigned to a given order could shift between runs
        # whenever an earlier iteration didn't need to create anything,
        # which broke idempotency (a rerun could create a second record
        # for the same order under a different provider).
        share_count = 0
        share_providers = [SocialShareRecord.Provider.WHATSAPP, SocialShareRecord.Provider.X, SocialShareRecord.Provider.FACEBOOK]
        for i, order in enumerate(Order.objects.filter(status=Order.Status.PAID).order_by("created_at")[:3]):
            _, created = SocialShareRecord.objects.get_or_create(
                user=order.buyer, listing=order.listing, order=order, provider=share_providers[i % 3],
            )
            share_count += created

        # -- A listing revision (pre-edit snapshot) --
        revision_listing = listings.get("Port expansion approved for Dar es Salaam")
        if revision_listing and not revision_listing.revisions.exists():
            NewsListingRevision.objects.create(
                listing=revision_listing, edited_by=revision_listing.seller,
                snapshot={
                    "title": revision_listing.title,
                    "description": "Government reviews a proposed infrastructure investment at the port.",
                    "body": revision_listing.body,
                    "price": str(revision_listing.price),
                    "location": revision_listing.location,
                },
            )

        # -- A subscription (active) + one cancelled, with transactions --
        sub, created = Subscription.objects.get_or_create(
            subscriber=users["demo_zainab"], seller=users["demo_amina"], status=Subscription.Status.ACTIVE,
            defaults={"price": Decimal("15000"), "current_period_end": timezone.now() + timedelta(days=25)},
        )
        if created:
            SubscriptionTransaction.objects.create(
                subscription=sub, selcom_order_id=f"SUBDEMO{sub.id.hex[:16].upper()}",
                reference=f"SUB-{sub.id.hex[:12]}-demo", channel=SubscriptionTransaction.Channel.MOBILE_MONEY,
                msisdn=users["demo_zainab"].phone_number or "+255712345678", amount=sub.price, currency="TZS",
                status=SubscriptionTransaction.Status.SUCCESS, selcom_transaction_id=f"SELCOM-SUB-DEMO-{sub.id.hex[:8]}",
                completed_at=timezone.now() - timedelta(days=5),
            )
            send_notification(
                user=sub.subscriber, notification_type=Notification.NotificationType.SUBSCRIPTION_ACTIVATED,
                title=f"You're now subscribed to {sub.seller.username}",
                message="You'll get every new verified story they publish for the next 30 days.",
                link_path=f"dashboard.html?tab=subscriptions", send_email=False,
            )
        cancelled_sub, created = Subscription.objects.get_or_create(
            subscriber=users["demo_john"], seller=users["demo_baraka"], status=Subscription.Status.CANCELLED,
            defaults={"price": Decimal("12000"), "current_period_end": timezone.now() - timedelta(days=3),
                      "cancelled_at": timezone.now() - timedelta(days=10)},
        )
        if created:
            SubscriptionTransaction.objects.create(
                subscription=cancelled_sub, selcom_order_id=f"SUBDEMOOLD{cancelled_sub.id.hex[:14].upper()}",
                reference=f"SUB-{cancelled_sub.id.hex[:12]}-old", channel=SubscriptionTransaction.Channel.MOBILE_MONEY,
                msisdn=users["demo_john"].phone_number or "+255712345678", amount=cancelled_sub.price, currency="TZS",
                status=SubscriptionTransaction.Status.SUCCESS, selcom_transaction_id=f"SELCOM-SUB-OLD-{cancelled_sub.id.hex[:8]}",
                completed_at=timezone.now() - timedelta(days=33),
            )

        # -- Seller payout accounts, mirroring the KYC verified/pending/rejected spread --
        payout_defs = [
            ("demo_amina", PayoutAccount.Status.VERIFIED, "M-Pesa"),
            ("demo_baraka", PayoutAccount.Status.PENDING, "Tigo Pesa"),
            ("demo_grace", PayoutAccount.Status.REJECTED, "Airtel Money"),
        ]
        for seller_key, status, provider in payout_defs:
            seller = users[seller_key]
            account, created = PayoutAccount.objects.get_or_create(
                user=seller, account_number=seller.phone_number or "+255712345678",
                defaults={
                    "account_type": PayoutAccount.AccountType.MOBILE_MONEY, "provider": provider,
                    "account_name": f"{seller.first_name} {seller.last_name}".strip() or seller.username,
                    "is_default": True, "status": status,
                    "reviewed_by": users["moderator1"] if status != PayoutAccount.Status.PENDING else None,
                    "reviewed_at": timezone.now() if status != PayoutAccount.Status.PENDING else None,
                    "rejection_reason": "Registered name does not match KYC name on file." if status == PayoutAccount.Status.REJECTED else "",
                },
            )

        # -- Company (platform treasury) payout account + one withdrawal request --
        company_account, _ = CompanyPayoutAccount.objects.get_or_create(
            account_number="0150-DEMO-TREASURY-001",
            defaults={
                "account_type": PayoutAccount.AccountType.BANK_ACCOUNT, "provider": "CRDB Bank",
                "account_name": "WEMIX Ltd", "is_active": True, "added_by": users["super_admin"],
            },
        )
        platform_wallet = get_or_create_platform_wallet()
        if platform_wallet.balance > 0:
            CompanyWithdrawalRequest.objects.get_or_create(
                payout_account=company_account, reason="Monthly commission sweep to company treasury.",
                defaults={
                    "amount": min(platform_wallet.balance, Decimal("10000")),
                    "requested_by": users["admin"], "status": CompanyWithdrawalRequest.Status.REQUESTED,
                },
            )

        # -- 2FA recovery codes for the one demo account with 2FA enabled --
        import hashlib

        two_fa_user = users["demo_john"]
        if not TwoFactorRecoveryCode.objects.filter(user=two_fa_user).exists():
            for i in range(5):
                code = f"DEMO-{two_fa_user.id.hex[:4].upper()}-{i}"
                TwoFactorRecoveryCode.objects.create(
                    user=two_fa_user, code_hash=hashlib.sha256(code.encode()).hexdigest(),
                    used_at=timezone.now() - timedelta(days=1) if i == 0 else None,
                )

        # -- A corporate (KYB) verification for a buyer acting on behalf of an org --
        corp_buyer = users["demo_zainab"]
        if not CorporateVerification.objects.filter(user=corp_buyer).exists():
            doc_text = (
                "WEMIX DEMO -- placeholder business document, not a real filing.\n"
                "This file exists only so the CorporateVerification demo row has real, "
                "viewable attachments instead of empty FileFields."
            )
            corp = CorporateVerification(
                user=corp_buyer, company_name="Zainab Media Consulting", status=CorporateVerification.Status.PENDING,
            )
            corp.business_license.save("business_license_demo.txt", ContentFile(doc_text.encode()), save=False)
            corp.company_registration.save("company_registration_demo.txt", ContentFile(doc_text.encode()), save=False)
            corp.save()

        # -- A support ticket with a staff reply thread --
        ticket, created = SupportTicket.objects.get_or_create(
            user=users["demo_fatuma"], subject="Refund not received after 5 days",
            defaults={
                "category": SupportTicket.Category.REFUND_HELP,
                "message": "I requested a refund last week for a story that didn't match its description. "
                            "It's been five days and I haven't seen the money back in my wallet. Can someone check?",
                "status": SupportTicket.Status.IN_PROGRESS, "assigned_to": users["moderator2"],
            },
        )
        if created:
            SupportTicketMessage.objects.create(
                ticket=ticket, author=users["moderator2"], is_staff_reply=True,
                message="Thanks for flagging this -- I can see your refund request in the queue, it's been "
                        "approved and is processing on our end. It should reflect in your wallet within 24 hours.",
            )
            SupportTicketMessage.objects.create(
                ticket=ticket, author=users["demo_fatuma"], is_staff_reply=False,
                message="Got it, thank you for the quick response!",
            )
            send_notification(
                user=ticket.user, notification_type=Notification.NotificationType.SUPPORT_TICKET_REPLY,
                title="New reply on your support ticket", message="A moderator replied to \"Refund not received after 5 days\".",
                link_path="dashboard.html?tab=support", send_email=False,
            )

        # -- An API key for a media house doing programmatic ingest --
        media_house = users["demo_starmedia"]
        if not APIKey.objects.filter(user=media_house, name="Newsroom ingest script").exists():
            APIKey.create_for_user(media_house, "Newsroom ingest script")

        # -- Terms acceptance, one per demo user --
        from django.conf import settings as django_settings

        terms_count = 0
        for user in users.values():
            _, created = TermsAcceptance.objects.get_or_create(
                user=user, version=django_settings.TERMS_VERSION, defaults={"ip_address": "41.222.10.5"},
            )
            terms_count += created

        # -- A handful of extra notifications covering event types reviews don't --
        notif_defs = [
            (users["demo_amina"], Notification.NotificationType.KYC_APPROVED, "Your identity verification was approved",
             "You can now sell news on WEMIX.", "dashboard.html?tab=verification", True),
            (users["demo_amina"], Notification.NotificationType.LISTING_APPROVED, "Your listing was approved",
             "\"Port expansion approved for Dar es Salaam\" is now live and purchasable.", "dashboard.html?tab=listings", True),
            (users["demo_grace"], Notification.NotificationType.SALE_COMPLETED, "You made a sale",
             "Someone just purchased \"New telecom tower rollout reaches rural Dodoma\".", "dashboard.html?tab=listings", False),
            (users["demo_amina"], Notification.NotificationType.WITHDRAWAL_COMPLETED, "Withdrawal completed",
             "Your withdrawal has been sent to your registered mobile money account.", "dashboard.html?tab=withdrawals", True),
            (users["demo_hamisi"], Notification.NotificationType.MODERATION_ACTION, "Your account was suspended",
             "A moderator has temporarily suspended your account for a platform policy violation.", "dashboard.html", False),
        ]
        notif_count = 0
        for user, ntype, title, message, link_path, is_read in notif_defs:
            if Notification.objects.filter(user=user, notification_type=ntype, title=title).exists():
                continue
            n = send_notification(user=user, notification_type=ntype, title=title, message=message, link_path=link_path, send_email=False)
            if is_read:
                n.is_read = True
                n.read_at = timezone.now()
                n.save(update_fields=["is_read", "read_at"])
            notif_count += 1

        self.stdout.write(
            f"  {review_count} reviews, {bookmark_count} bookmarks, {follow_count} follows, "
            f"{share_count} social shares, {terms_count} terms acceptances, {notif_count} extra notifications"
        )

    # ------------------------------------------------------------------
    def seed_core(self):
        self.stdout.write("Seeding platform settings...")
        settings_defs = [
            ("platform_commission_rate", 0.10, "Default commission rate taken on each sale."),
            ("ai_auto_verify_threshold", 20.0, "Fake-news score below which content auto-verifies."),
            ("maintenance_mode", False, "When true, the platform shows a maintenance banner."),
        ]
        for key, value, desc in settings_defs:
            PlatformSetting.objects.get_or_create(key=key, defaults={"value": value, "description": desc})

        # A handful of illustrative audit log entries beyond what the
        # service-layer calls above already generated.
        admin = User.objects.filter(username="demo_admin").first()
        if admin:
            AuditLog.objects.get_or_create(
                actor=admin, action=AuditLog.Action.SETTINGS_CHANGE, target_model="PlatformSetting",
                target_id="platform_commission_rate",
                defaults={"description": "Initial platform settings configured via seed script."},
            )

    # ------------------------------------------------------------------
    def print_summary(self):
        rows = [
            ("Users", User.objects.count()),
            ("  - Identity verifications", IdentityVerification.objects.count()),
            ("  - Login sessions", LoginSession.objects.count()),
            ("  - Two-factor recovery codes", TwoFactorRecoveryCode.objects.count()),
            ("  - Corporate (KYB) verifications", CorporateVerification.objects.count()),
            ("Categories", Category.objects.count()),
            ("Tags", Tag.objects.count()),
            ("News listings", NewsListing.objects.count()),
            ("  - AI verification results", AIVerificationResult.objects.count()),
            ("  - Media", NewsMedia.objects.count()),
            ("  - Image analysis results", ImageAnalysisResult.objects.count()),
            ("  - Listing revisions", NewsListingRevision.objects.count()),
            ("  - Reviews", Review.objects.count()),
            ("  - Bookmarks", Bookmark.objects.count()),
            ("  - Follows", Follow.objects.count()),
            ("  - Saved searches", SavedSearch.objects.count()),
            ("  - Corrections", Correction.objects.count()),
            ("  - Social share records", SocialShareRecord.objects.count()),
            ("Orders", Order.objects.count()),
            ("  - Selcom transactions", SelcomTransaction.objects.count()),
            ("  - Nala (international) transactions", NalaTransaction.objects.count()),
            ("  - Webhook logs", PaymentWebhookLog.objects.count()),
            ("  - Refund requests", RefundRequest.objects.count()),
            ("Subscriptions", Subscription.objects.count()),
            ("  - Subscription transactions", SubscriptionTransaction.objects.count()),
            ("Wallets", Wallet.objects.count()),
            ("  - Wallet transactions (ledger entries)", WalletTransaction.objects.count()),
            ("  - Withdrawal requests", WithdrawalRequest.objects.count()),
            ("  - Seller payout accounts", PayoutAccount.objects.count()),
            ("  - Company payout accounts", CompanyPayoutAccount.objects.count()),
            ("  - Company withdrawal requests", CompanyWithdrawalRequest.objects.count()),
            ("Anti-circumvention flags", AntiCircumventionFlag.objects.count()),
            ("User violation histories", UserViolationHistory.objects.count()),
            ("User reports", UserReport.objects.count()),
            ("Moderation queue items", ModerationQueueItem.objects.count()),
            ("Platform settings", PlatformSetting.objects.count()),
            ("Audit log entries", AuditLog.objects.count()),
            ("Support tickets", SupportTicket.objects.count()),
            ("  - Support ticket messages", SupportTicketMessage.objects.count()),
            ("API keys", APIKey.objects.count()),
            ("Terms acceptances", TermsAcceptance.objects.count()),
            ("Notifications", Notification.objects.count()),
        ]
        self.stdout.write("\nTable counts after seeding:")
        for label, count in rows:
            self.stdout.write(f"  {label}: {count}")

        self.stdout.write(f"\nAll demo users share the password: {DEMO_PASSWORD}")
        self.stdout.write("Notable logins:")
        self.stdout.write("  demo_superadmin  -- super admin (Django admin + everything)")
        self.stdout.write("  demo_moderator1  -- moderator (moderation queue access)")
        self.stdout.write("  demo_amina       -- verified journalist/seller with sales + wallet balance")
        self.stdout.write("  demo_hamisi      -- seller, KYC pending, currently suspended")
        self.stdout.write("  demo_neema       -- seller, KYC rejected, has circumvention flags")
        self.stdout.write("  demo_john        -- buyer, 2FA enabled, has purchases")
