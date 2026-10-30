# HELIX’S STUDIO — Private 18+ Fan Membership Portal

A **separate** web project, not a replacement for the existing Nova Investment site. This is a FastAPI + SQLite website with the uploaded **HELIX STUDIOS** logo and a black/silver responsive interface. Seven prepaid tier prices: $200, $300, $500, $1,000, $3,000, $5,000 and $10,000.

## Quickest way to see the design

Extract the ZIP on Windows, then double-click **`OPEN_PREVIEW.html`** to see the home page and plan cards without installing Python. This is **only a visual preview**; real signup, verification, payments and administrator controls require the running Python application.

## Included, working features

- Desktop/mobile navigation, home, membership cards, content-tier placeholders, age-entry declaration and terms.
- Create account, hashed passwords, **one account per email** enforced by the database (case-insensitive), and six-digit email code confirmation.
- Verification on **every login**, password-reset verification, short code expiration, one-time use, limited attempts and a resend delay.
- Customer dashboard: membership level, payment status/history, private support message thread and staff replies.
- Manual **Bitcoin payment requests** (requires a configured real BTC wallet address), and **gift-card payment requests** (disabled until an approved external redemption process is configured). Users submit transaction or safe merchant receipt references; optional image receipts are stored privately, not in the public static directory.
- Admin panel at `/admin`: customer directory, suspend/restore non-admin accounts, pending payment queue, private payment proof, manually approve/reject and annotate each request, and answer member support messages.
- **Membership activates only after an admin confirms independent payment verification**. No real cryptocurrency or gift-card money is processed or redeemed by this application.
- Database and receipt storage paths configurable for a separate Render persistent disk.
- Strict 18+ messaging; actual adult media has NOT been added.

## Before public launch — especially important for an 18+ business

1. **Do not accept real payments until you finalize and publish refund, cancellation, delivery, tax, crypto conversion and billing terms.** The project currently assumes 12 months' prepaid access and no automatic renewal; confirm this is what you want. Merchandise, creator access and event eligibility are only proposed until you can fulfill them.
2. **Age checkbox is only a declaration, not formal age verification.** Implement a legal age-and-identity verification process appropriate to your audience, location and media before hosting/distributing regulated adult content. Verify creators are adults and have consented, and obtain distribution rights.
3. Ensure that you own/are authorized to use the brand and any celebrities' names, photos and likenesses. No private meetings, intimacy or sexual services are guaranteed by memberships.
4. Configure and test secure transactional email SMTP. Recipients can receive codes in Gmail regardless of which legitimate SMTP provider you use. Choose an email provider that permits the platform's content category. The old disabled Gmail account is not required.
5. For BTC, verify the real destination wallet and network details yourself and independently check the on-chain transaction and amount before approving. The website does not generate exchange rates, detect transactions, or verify payment automatically.
6. **Gift-card purchases are high-risk** and issuer terms may disallow their use for third-party purchases. The site never requests gift-card codes/PINs. Turn the gift-card method on only after establishing a lawful, documented, independently verifiable merchant redemption or settlement workflow. Do not solicit PINs through chat, email, uploads or the form.
7. Private proof uploads are restricted and re-encoded, but a production launch needs a security review, backups, logs, rate limits, retention policies, abuse monitoring, independent age verification, consumer law review, and clear privacy policy.
8. The tests use test credentials and mocked email delivery, **not** a full production security audit.

## Run locally (Windows PowerShell)

From the extracted `helix_studio_portal` folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ENVIRONMENT="development"
$env:EMAIL_MODE="console"
$env:SECRET_KEY="some-long-random-dev-only-secret-123456789"
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000`. In this LOCAL development setup, verification codes are printed in the PowerShell terminal (not sent to inbox). Never enable console mode on a public website. Set `ADMIN_EMAIL` and a unique `ADMIN_PASSWORD` (12+ characters) **before the first application start** to create an admin account. Admin signs in with email and password, then an email code, just like a member. Tests run with `python -m unittest discover -s tests -v`.

## Deploy separately to Render — do not reuse the Nova service

1. Create a **new** GitHub repository for Helix's Studio and upload the contents of this ZIP. Do not upload a ZIP as one repository file; upload `main.py`, `templates/`, `static/`, `requirements.txt`, `render.yaml`, and so on at the repository root. Do **not** modify `nova-investment`.
2. Create a new Render web service (or Blueprint from `render.yaml`) linked to the Helix repo. Use a **paid web instance that supports persistent disks** if you expect account/payment data to survive deployment. Use mount path `/var/data` for the Helix database, not the existing Nova disk. The sample Render Blueprint is a reference; review plan availability and costs before creating a service.
3. Set environment variables. **Never commit secrets**. Required in production:
   - `ENVIRONMENT=production`
   - `SECRET_KEY` securely generated at least 32 characters, **keep it stable** (rotating logs everybody out)
   - `DATABASE_PATH=/var/data/helix_studio.db`
   - `PRIVATE_UPLOAD_DIR=/var/data/helix_proofs`
   - `ADMIN_EMAIL` = your own reachable address
   - `ADMIN_PASSWORD` = unique strong 12+ character password
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` = verified SMTP credentials
   - `SUPPORT_EMAIL` = your monitored support inbox
4. For Bitcoin, set `BTC_ADDRESS` to your own verified receiver address. Gift cards require `GIFT_CARD_ENABLED=1` **and** `GIFT_CARD_INSTRUCTIONS` only after verified permitted redemption arrangements exist. Both are off by default.
5. Standard Render build: `pip install -r requirements.txt`. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers`. Health check: `/health`.
6. After setup, verify register → inbox OTP → login OTP → dashboard → pending payment request → private admin review → manual approval → member access; also test from a phone screen.
7. Enable secure regular backups of `/var/data/helix_studio.db` with SQLite's backup API; proof files are also in `/var/data`. Consider a managed database and proper file storage before a real high-traffic launch. Avoid modifying live database files without a backup.

## Files

- `main.py`: full app including account flows, payment requests, admin routes and DB schema.
- `templates/`: responsive website views and forms.
- `static/styles.css`, `static/site.js`: desktop/mobile presentation and checkout method switcher.
- `static/helix-logo.png`: your uploaded logo.
- `render.yaml`: example for a separate Helix Render service.
- `.env.example`: local configuration template; copy to your own private `.env` if desired, and never commit real credentials.
- `tests/test_portal.py`: local integration tests of basic flows and admin permissions.

**Status:** Working local application prototype, not a deployed production service. No email has been sent to real Gmail accounts, no real BTC transfers or gift-card redemptions have been tested, and no live user/customer records have been created by this package.
