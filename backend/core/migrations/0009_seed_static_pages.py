from django.db import migrations

PAGES = [
    {
        "slug": "about",
        "title": "About Us",
        "meta_description": "WEMIX is a verified news marketplace connecting journalists, media houses, and buyers.",
        "body": (
            "WEMIX (Habari Platform) is a marketplace where verified journalists, media houses, "
            "and independent sellers publish fact-checked news for sale, and buyers purchase "
            "direct access to it.\n\n"
            "Every listing passes through an AI verification pipeline and, where flagged, human "
            "moderator review before it goes live. Sellers are identity-verified before they can "
            "publish. We're an early-stage platform, built from Dar es Salaam, Tanzania, with a "
            "global marketplace in mind."
        ),
    },
    {
        "slug": "careers",
        "title": "Careers",
        "meta_description": "Careers at WEMIX -- open roles and how to reach us.",
        "body": (
            "We're a small, early-stage team. We don't have a formal careers page or open "
            "requisitions posted yet -- if you're interested in working on a verified-news "
            "marketplace, reach out via the contact details on this site and tell us what you'd "
            "want to work on."
        ),
    },
    {
        "slug": "press-center",
        "title": "Press Center",
        "meta_description": "Press and media inquiries for WEMIX.",
        "body": (
            "For press inquiries about WEMIX -- the platform, not the individually-reported "
            "stories sold on it -- contact us through the details listed in our footer. We'll "
            "get back to you as soon as we can; we're a small team without a dedicated press line yet."
        ),
    },
    {
        "slug": "faq",
        "title": "FAQ",
        "meta_description": "Frequently asked questions about buying and selling on WEMIX.",
        "body": (
            "How do I buy a story? Create an account, find a listing, and purchase it -- "
            "payment unlocks the full article and any attached media instantly.\n\n"
            "How do I sell a story? Register as a Journalist, Seller, or Media House, complete "
            "identity verification, and submit a listing. It goes through AI and, where flagged, "
            "human review before publishing.\n\n"
            "How do refunds work? Request one from your dashboard's Purchases tab, stating a "
            "reason. A moderator reviews every request -- refunds aren't automatic.\n\n"
            "How do sellers get paid? Sales are credited to your in-platform wallet after the "
            "platform commission is deducted. Withdraw to a verified payout account from your "
            "dashboard.\n\n"
            "More questions? Use the Support tab in your dashboard once signed in."
        ),
    },
    {
        "slug": "privacy-policy",
        "title": "Privacy Policy",
        "meta_description": "How WEMIX collects, uses, and protects your data.",
        "body": (
            "This is a working draft appropriate for an early-stage platform and has not yet "
            "been reviewed by outside counsel.\n\n"
            "We collect the information you provide at signup (name, email, phone), identity "
            "verification documents (for sellers), and records of your activity on the platform "
            "(purchases, listings, support tickets) needed to operate the marketplace. Payment "
            "details are handled by our payment providers (Selcom, Nala) -- we don't store your "
            "card or mobile money credentials ourselves.\n\n"
            "We don't sell your personal data. It's used to operate your account, process "
            "payments, run identity verification, and comply with legal obligations. You can "
            "request an export of your own data from your dashboard, or request account "
            "deactivation."
        ),
    },
    {
        "slug": "cookie-policy",
        "title": "Cookie Policy",
        "meta_description": "How WEMIX uses cookies and local storage.",
        "body": (
            "WEMIX uses browser local storage (not third-party tracking cookies) to keep you "
            "signed in between visits -- your session token is stored in your browser's "
            "localStorage, not a cookie. We don't currently use third-party advertising or "
            "analytics trackers on this site."
        ),
    },
]


def seed_pages(apps, schema_editor):
    StaticPage = apps.get_model("core", "StaticPage")
    for page in PAGES:
        StaticPage.objects.get_or_create(slug=page["slug"], defaults={
            "title": page["title"],
            "body": page["body"],
            "meta_description": page["meta_description"],
            "is_published": True,
        })


def unseed_pages(apps, schema_editor):
    StaticPage = apps.get_model("core", "StaticPage")
    StaticPage.objects.filter(slug__in=[p["slug"] for p in PAGES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_staticpage"),
    ]

    operations = [
        migrations.RunPython(seed_pages, unseed_pages),
    ]
