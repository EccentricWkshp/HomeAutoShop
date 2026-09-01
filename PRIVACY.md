# HomeAutoShop Privacy Policy

Effective date: September 1, 2026

## Overview

HomeAutoShop is self-hosted, local-first software. The project maintainers do
not operate a HomeAutoShop service and do not receive the records stored in an
installation. Each person or organization that runs an instance controls that
instance, its users, its storage, and any optional services connected to it.

HomeAutoShop contains no advertising, analytics, telemetry, crash-reporting
service, or automatic update checker. It does not sell personal information.
Some features make network requests when an instance administrator enables
them or a user deliberately invokes them. Those cases are described below.

## Information HomeAutoShop processes

Depending on how an instance is used, HomeAutoShop may process:

- Account information, such as a username, optional name and email address,
  role, language and unit preferences, password hash, session data, and hashed
  API tokens.
- People and ownership records, including names, contact details, addresses,
  notes, household status, vehicle assignments, and access grants entered by
  users.
- Vehicle and equipment information, such as VINs, license plates, serial
  numbers, specifications, ownership history, meter readings, and recall
  information.
- Shop records, including work orders, notes, inspections, maintenance
  schedules, diagnostic reports and trouble codes, parts, inventory,
  purchases, vendors, expenses, time entries, and related history.
- Files supplied by users, such as photographs, receipts, registrations,
  insurance documents, manuals, audio notes, and scan-tool reports. The
  application may create thumbnails, previews, file hashes, and locally
  extracted OCR text from those files.
- Operational information, including settings, encrypted integration
  credentials, job status and errors, sign-in and change history, outbound
  request history, notification destinations and delivery history, and Web
  Push subscription data.
- Web-server access logs. The standard deployment logs request information
  such as the requesting IP address, date and time, request method and path,
  response status, referrer, and browser user agent to the instance's own
  container logs.

HomeAutoShop uses this information to provide authentication, authorization,
recordkeeping, search, reports, offline operation, backups and exports,
notifications, diagnostics, and the optional integrations selected by the
instance administrator.

## Information stored on a browser or device

HomeAutoShop uses first-party session and CSRF cookies for sign-in and request
security. These are functional cookies, not advertising or analytics cookies.

The Progressive Web App may store recently visited pages, API responses, and
thumbnails in the browser cache so they remain available offline. Uploaded
original files are not placed in that cache. Writes made while offline,
including their field values, are stored in the browser's IndexedDB until they
sync, are resolved after a conflict, or are discarded. A shared or lost device
can therefore contain readable shop information even when it cannot reach the
server. Clearing the site's browser data removes these local copies.

VIN and barcode scanning uses the device camera through browser features.
ELM327 communication uses the browser's serial-device support. Camera frames
and raw serial traffic are processed on the device; only values a user chooses
to save are submitted to the HomeAutoShop instance.

If a user enables Web Push, the browser creates a subscription containing a
push-service endpoint and cryptographic keys. HomeAutoShop stores that
subscription so it can deliver notifications. Notification payloads sent by
HomeAutoShop contain a generic title, an item count, and a link back to the
instance; they do not name a vehicle or include the detailed reminder list.

## Storage, backups, and exports

The instance stores structured data in its configured SQLite or PostgreSQL
database. By default, uploaded media and generated derivatives are stored on
the instance's filesystem. An administrator may instead configure an
S3-compatible object store, in which case that storage provider receives and
stores the media.

Uploaded originals are preserved unchanged. Generated image thumbnails and
previews omit EXIF metadata, including GPS metadata, but the original upload
may still contain its original EXIF and location data. Users who do not want
that information retained should remove it from the original before uploading
the file.

Backups contain the database and, when filesystem storage is used, media files.
Portable exports contain application records and media. Password hashes, API
token hashes, and stored integration credentials are omitted from portable
exports; stored integration credentials are also removed from application-
generated database backups. Integration credentials stored in the live
database are encrypted. Backups and exports can nevertheless contain extensive
private information and should be protected accordingly. When S3-compatible
storage is used, application-generated backups do not include the objects in
that store; the administrator is responsible for backing them up separately.

## Optional network requests and disclosures

HomeAutoShop permits server-initiated HTTP requests only to configured or
allowlisted hosts and records the host, path, purpose, result, duration, and
requesting user, when available, in its local audit log. Query strings are not
recorded by the common outbound-request logger. Plate lookups additionally
record the plate and region locally as part of the lookup audit history.

Optional network activity includes:

- **VIN decoding:** Looking up a modern vehicle sends the full VIN and, when
  available, model year to the configured vPIC service. The response is stored
  with the vehicle record.
- **Recall checks:** A recall check sends year, make, and model to the NHTSA
  recall service, not the VIN. Following the separate VIN-level recall link in
  a browser sends the VIN to the external NHTSA website.
- **License-plate lookup:** This feature is off by default, requires a provider
  chosen by the administrator, and asks for confirmation before each call. It
  sends the plate and issuing state or province to that provider.
- **LubeLogger:** When configured, HomeAutoShop sends an API credential and
  requests vehicle, service, expense, tax, fuel, upgrade, odometer, and related
  records. An optional write-back mode can send an odometer value, date, and
  note to LubeLogger.
- **WrenchLedger:** When configured, HomeAutoShop sends an API credential and
  requests workspace, tool, availability, and related sync information.
  User-entered tool search text may be included in those requests.
- **Shared catalogs:** When an administrator configures a catalog, HomeAutoShop
  requests its index and selected template files and caches the results.
- **Notifications:** Email reminders go through the configured SMTP server and
  include the detailed reminder digest. Webhook reminders send the detailed
  digest to the configured webhook. Web Push sends the limited payload
  described above through the browser vendor's push service.
- **Object storage:** Configuring an S3-compatible store sends uploaded media
  and storage operations to that store.
- **TLS certificates:** The optional ACME DNS configuration communicates with
  the selected certificate authority and DNS provider. Issued hostnames may be
  published in Certificate Transparency logs. This traffic is handled by the
  reverse proxy, not by the HomeAutoShop application.

External providers receive ordinary connection metadata, including the
instance's public IP address and request time, and apply their own privacy and
retention terms. Links to service-information sites, product sites, or other
external pages are opened by the user's browser and are also governed by the
destination site's policies.

The application's Offline Mode blocks application-initiated integration,
notification, and Web Push traffic. It does not block a user's browser from
opening an external link, access to an administrator-configured object store,
or the reverse proxy's certificate-management traffic.

## Sharing and access

The project maintainers do not have access to an instance or its records.
Information is available to the instance administrator, authorized users, the
infrastructure providers selected by the administrator, and the optional
services described above. Role and vehicle-level access controls limit what
signed-in users can access, but administrators are responsible for configuring
accounts and permissions appropriately.

HomeAutoShop does not sell, rent, or use stored information for advertising or
profiling. It does not send records to the project maintainers.

## Retention and deletion

The instance administrator controls the lifetime of the database, media,
exports, backups, logs, browser data, and external storage.

Most deleted application records are soft-deleted and remain restorable in the
trash for 30 days. The current application does not automatically purge those
records from the database when the 30-day window ends. Append-only history and
audit records are intentionally retained to preserve the shop record. An
account that has authored history is normally deactivated rather than deleted;
an unused account with no associated history can be removed.

Application-generated backups use configurable daily, weekly, and monthly
retention. Manually downloaded backups and exports remain wherever the
administrator or user placed them until deleted there. Removing live data does
not remove copies already present in backups, exports, browser caches, external
systems, or provider logs. Those copies follow their own retention and deletion
processes.

## Choices and control

Subject to their role, users and administrators can review and correct records,
soft-delete supported records, manage accounts and notification channels,
disable integrations, enable Offline Mode, revoke API tokens, download backups
and exports, clear browser site data, and remove data from the underlying
storage. Requests about access, correction, export, or deletion should be
directed to the administrator of the relevant HomeAutoShop instance.

## Security

HomeAutoShop supports TLS, password hashing, HttpOnly session cookies, CSRF
protection, role-based access, private authenticated media routes, hashed API
tokens, encrypted stored integration credentials, outbound-host allowlisting,
and audit logging. No system can guarantee absolute security. The instance
administrator remains responsible for securing the host, database, object
store, backups, exports, network exposure, TLS configuration, user accounts,
and connected services.

## Changes to this policy

This policy may change as HomeAutoShop changes. The effective date above
identifies this version. An instance administrator should review policy and
configuration changes when updating the software.

## Questions

Questions about a specific installation or its records should be directed to
that installation's administrator. Questions about this policy or the software
can be raised through the project's repository without including private
records, credentials, or other sensitive information.
