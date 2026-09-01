from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from homeautoshop.accounts import views as accounts
from homeautoshop.api.urls import api
from homeautoshop.assets import views as assets
from homeautoshop.core import views as core
from homeautoshop.core import views_integrations as integrations
from homeautoshop.core import views_settings as instance_settings
from homeautoshop.core import views_setup
from homeautoshop.diagnostics import views as diagnostics
from homeautoshop.mediafiles import views as mediafiles
from homeautoshop.inspections import views as inspections
from homeautoshop.maintenance import views as maintenance
from homeautoshop.parts import views as parts
from homeautoshop.people import views as people
from homeautoshop.purchasing import views as purchasing
from homeautoshop.purchasing import views_import as purchasing_import
from homeautoshop.work import views as work

urlpatterns = [
    # Auth
    path(
        "login/",
        # Not Django's LoginView directly: on an instance that has never had an
        # account, this sends the first arrival to the setup page instead of
        # presenting a form nobody can possibly pass (FR-ADM-1).
        views_setup.FirstRunView.as_view(
            template_name="auth/login.html", redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Who may sign in (FR-ADM-2). No delete route exists on purpose: see
    # the module docstring in homeautoshop/accounts/views.py.
    path("users/", accounts.user_list, name="user_list"),
    path("users/new/", accounts.user_create, name="user_create"),
    path("users/<uuid:pk>/", accounts.user_detail, name="user_detail"),
    path("users/<uuid:pk>/signin/", accounts.user_set_active, name="user_set_active"),
    path("users/<uuid:pk>/password/", accounts.user_set_password, name="user_set_password"),
    path("users/<uuid:pk>/vehicles/", accounts.user_access, name="user_access"),
    path("users/<uuid:pk>/delete/", accounts.user_delete, name="user_delete"),
    # Reachable only while the accounts table is empty; see views_setup.
    path("setup/", views_setup.setup, name="setup"),
    # Dashboard and search
    path("", core.dashboard, name="dashboard"),
    path("search/", core.search_view, name="search"),
    path("health/", core.health, name="health"),
    path("reminders/", core.reminders, name="reminders"),
    path("reminders/channels/", core.reminder_channel_add, name="reminder_channel_add"),
    path("reminders/channels/<uuid:channel_id>/", core.reminder_channel_action,
         name="reminder_channel_action"),
    # Instance settings and backup (SPEC §17 R-9, R-10).
    path("settings/", instance_settings.settings_view, name="settings"),
    # Before the `<str:group>` pattern, which would otherwise match this and
    # 404 on an unknown group — the banner's button leading nowhere.
    path("settings/apply-restart/", instance_settings.settings_restart, name="settings_restart"),
    path("settings/<str:group>/", instance_settings.settings_view, name="settings"),
    path("backups/", instance_settings.backups, name="backups"),
    path("backups/run/", instance_settings.backup_now, name="backup_now"),
    path("backups/<str:name>/download/", instance_settings.backup_download, name="backup_download"),
    path("backups/<str:name>/delete/", instance_settings.backup_delete, name="backup_delete"),
    path("integrations/", integrations.integrations, name="integrations"),
    path("integrations/<str:name>/test/", integrations.integration_test, name="integration_test"),
    path("integrations/<str:name>/sync/", integrations.integration_sync, name="integration_sync"),
    path("integrations/dismiss-link/", integrations.dismiss_product_link,
         name="dismiss_product_link"),
    path("integrations/lubelogger/", core.lubelogger_import, name="lubelogger_import"),
    path("integrations/lubelogger/link/", core.lubelogger_link, name="lubelogger_link"),
    path("import/", integrations.data_import, name="data_import"),
    path("sync/", core.sync_queue, name="sync_queue"),
    # One label format for bins, vehicles and parts alike (SPEC FR-INV-2).
    path("s/<uuid:pk>/", core.scan_target, name="scan_target"),
    path("labels/", core.labels, name="labels"),
    path("reminders/push/", core.push_subscribe, name="push_subscribe"),
    # The worker is served from the root so its scope covers the whole app.
    path("sw.js", core.service_worker, name="service_worker"),
    # Uploaded files are served by the application, not linked straight to the
    # object store: a presigned URL names a host only the containers can
    # resolve, and is readable by anyone who copies it. See mediafiles/views.py.
    # Detaching a file from one record, which is not the same as deleting it:
    # a receipt hangs off both a purchase and a work order.
    path("files/links/<uuid:link_id>/remove/", mediafiles.media_unlink, name="media_unlink"),
    path("files/<uuid:pk>/", mediafiles.media_file, name="media_file"),
    path("files/<uuid:pk>/<str:variant>/", mediafiles.media_file, name="media_file_variant"),
    path("trash/", core.trash, name="trash"),
    path("trash/<str:kind>/<uuid:pk>/restore/", core.trash_restore, name="trash_restore"),
    path("healthz", core.healthz, name="healthz"),
    path("readyz", core.readyz, name="readyz"),
    # Assets
    path("vehicles/", assets.asset_list, name="asset_list"),
    path("vehicles/new/", assets.asset_create, name="asset_create"),
    path("vehicles/vin-check/", assets.vin_validate, name="vin_validate"),
    path("vehicles/plate-lookup/", assets.plate_lookup, name="plate_lookup"),
    path("vehicles/<uuid:pk>/", assets.asset_detail, name="asset_detail"),
    path("vehicles/<uuid:pk>/edit/", assets.asset_edit, name="asset_edit"),
    # For a vehicle added twice, not for one that was sold — a sold car keeps
    # its history, and `status = sold` is what says so.
    path("vehicles/<uuid:pk>/delete/", assets.asset_delete, name="asset_delete"),
    path("vehicles/<uuid:pk>/decode/", assets.vin_decode, name="vin_decode"),
    path("vehicles/<uuid:pk>/read-vin/", assets.vin_read, name="vin_read"),
    path("vehicles/<uuid:pk>/readings/", assets.reading_create, name="reading_create"),
    path("vehicles/<uuid:pk>/photos/", assets.photo_upload, name="asset_photo_upload"),
    path("vehicles/<uuid:pk>/documents/", assets.document_upload, name="asset_document_upload"),
    path("vehicles/<uuid:pk>/links/", assets.link_add, name="asset_link_add"),
    path("vehicles/<uuid:pk>/links/<uuid:link_id>/remove/", assets.link_delete, name="asset_link_delete"),
    path(
        "vehicles/<uuid:pk>/manuals/<uuid:provider_id>/pin/",
        assets.service_info_pin,
        name="service_info_pin",
    ),
    path(
        "vehicles/<uuid:pk>/manuals/<uuid:provider_id>/unpin/",
        assets.service_info_unpin,
        name="service_info_unpin",
    ),
    path(
        "vehicles/<uuid:pk>/manuals/<uuid:provider_id>/visibility/",
        assets.service_info_visibility,
        name="service_info_visibility",
    ),
    path("vehicles/<uuid:pk>/owners/", assets.ownership_add, name="ownership_add"),
    path(
        "vehicles/<uuid:pk>/owners/<uuid:ownership_id>/end/",
        assets.ownership_end,
        name="ownership_end",
    ),
    # Work orders
    path("work-orders/", work.work_order_list, name="work_order_list"),
    path("work-orders/new/", work.work_order_create, name="work_order_create"),
    path("work-orders/<uuid:pk>/", work.work_order_detail, name="work_order_detail"),
    path("work-orders/<uuid:pk>/edit/", work.work_order_edit, name="work_order_edit"),
    path("work-orders/<uuid:pk>/status/", work.work_order_transition, name="work_order_transition"),
    path("work-orders/<uuid:pk>/delete/", work.work_order_delete, name="work_order_delete"),
    path("work-orders/<uuid:pk>/notes/", work.note_create, name="note_create"),
    path("work-orders/<uuid:pk>/items/", work.job_item_create, name="job_item_create"),
    path("work-orders/<uuid:pk>/items/<uuid:item_id>/toggle/", work.job_item_toggle, name="job_item_toggle"),
    # A list of work changes as the work does: items get reworded, reassigned,
    # skipped, and done in a different order from the one they were written in.
    path("work-orders/<uuid:pk>/items/<uuid:item_id>/edit/", work.job_item_edit,
         name="job_item_edit"),
    path("work-orders/<uuid:pk>/items/<uuid:item_id>/move/", work.job_item_move,
         name="job_item_move"),
    path("work-orders/<uuid:pk>/items/<uuid:item_id>/delete/", work.job_item_delete,
         name="job_item_delete"),
    path("work-orders/<uuid:pk>/photos/", work.work_order_photo, name="work_order_photo"),
    path("work-orders/<uuid:pk>/parts/", work.part_use, name="work_order_part_use"),
    # Needing a part and using one are different acts: the first is a claim
    # made while the job can still be planned around, the second moves stock.
    path("work-orders/<uuid:pk>/parts/needed/", work.part_require, name="work_order_part_require"),
    path(
        "work-orders/<uuid:pk>/parts/needed/<uuid:requirement_id>/remove/",
        work.part_unrequire,
        name="work_order_part_unrequire",
    ),
    path(
        "work-orders/<uuid:pk>/parts/needed/order/",
        work.part_order_shortfall,
        name="work_order_order_shortfall",
    ),
    path("work-orders/<uuid:pk>/time/", work.time_add, name="work_order_time_add"),
    # Append-only never meant unremovable: a timer left running overnight
    # puts eleven hours on a job, and leaving it there costs the number.
    path("work-orders/<uuid:pk>/time/<uuid:entry_id>/edit/", work.time_entry_edit,
         name="time_entry_edit"),
    path("work-orders/<uuid:pk>/time/<uuid:entry_id>/delete/", work.time_entry_delete,
         name="time_entry_delete"),
    path("work-orders/<uuid:pk>/items/<uuid:item_id>/tools/", work.job_item_tool_add,
         name="job_item_tool_add"),
    path("work-orders/<uuid:pk>/tools/<uuid:reference_id>/remove/", work.job_item_tool_remove,
         name="job_item_tool_remove"),
    # Tool lookup for a job item, so nobody has to remember a WrenchLedger id.
    # A tool named on a job used to be unreachable afterwards: no list, no way
    # to correct it, no way to remove it, and no way to ask what the shop owns.
    path("tools/", work.tool_list, name="tool_list"),
    path("tools/<uuid:pk>/delete/", work.tool_delete, name="tool_delete"),
    path("tools/search/", work.tool_search, name="tool_search"),
    path("work-orders/<uuid:pk>/expenses/", purchasing.expense_add, name="work_order_expense_add"),
    path("expenses/<uuid:pk>/edit/", purchasing.expense_edit, name="expense_edit"),
    path("expenses/<uuid:pk>/delete/", purchasing.expense_delete, name="expense_delete"),
    # The vehicle page carries a summary of this; the full story lives here,
    # where nothing on the screen is competing with it.
    path("vehicles/<uuid:pk>/history/", assets.asset_timeline, name="asset_timeline"),
    path("vehicles/<uuid:pk>/specs/", assets.asset_specs, name="asset_specs"),
    path("vehicles/<uuid:pk>/specs/add/", assets.spec_add, name="spec_add"),
    path("vehicles/<uuid:pk>/specs/from-decode/", assets.spec_from_decode,
         name="spec_from_decode"),
    path("vehicles/<uuid:pk>/specs/from-scan/", assets.spec_from_scan, name="spec_from_scan"),
    path("vehicles/<uuid:pk>/specs/copy/", assets.spec_copy, name="spec_copy"),
    path("vehicles/<uuid:pk>/specs/<uuid:spec_id>/edit/", assets.spec_edit, name="spec_edit"),
    path("vehicles/<uuid:pk>/specs/<uuid:spec_id>/delete/", assets.spec_delete, name="spec_delete"),
    # Diagnostics (SPEC §8.3)
    path("diagnostics/", diagnostics.queue, name="diagnostic_queue"),
    path("diagnostics/profiles/", diagnostics.profile_list, name="profile_list"),
    path("diagnostics/profiles/import/", diagnostics.profile_import, name="profile_import"),
    path("diagnostics/profiles/<uuid:pk>.yaml", diagnostics.profile_export, name="profile_export"),
    path("diagnostics/profiles/<uuid:pk>/toggle/", diagnostics.profile_toggle, name="profile_toggle"),
    path("diagnostics/sessions/<uuid:pk>/", diagnostics.session_detail, name="session_detail"),
    path("diagnostics/sessions/<uuid:pk>/confirm/", diagnostics.session_confirm, name="session_confirm"),
    path("diagnostics/sessions/<uuid:pk>/discard/", diagnostics.session_discard, name="session_discard"),
    path("diagnostics/sessions/<uuid:pk>/reparse/", diagnostics.session_reparse, name="session_reparse"),
    path("diagnostics/sessions/<uuid:pk>/map/", diagnostics.session_map, name="session_map"),
    path("diagnostics/codes/<uuid:pk>/promote/", diagnostics.code_promote, name="code_promote"),
    path("diagnostics/codes/<uuid:pk>/status/", diagnostics.code_status, name="code_status"),
    path("diagnostics/codes/<uuid:pk>/describe/", diagnostics.code_describe, name="code_describe"),
    path("vehicles/<uuid:pk>/diagnostics/", diagnostics.asset_diagnostics, name="asset_diagnostics"),
    path("vehicles/<uuid:pk>/diagnostics/import/", diagnostics.session_import, name="session_import"),
    path("vehicles/<uuid:pk>/diagnostics/live/", diagnostics.elm327, name="elm327"),
    path("vehicles/<uuid:pk>/diagnostics/live/capture/", diagnostics.elm_capture, name="elm_capture"),
    path("vehicles/<uuid:pk>/recalls/", assets.asset_recalls, name="asset_recalls"),
    path("vehicles/<uuid:pk>/recalls/check/", assets.recall_check, name="recall_check"),
    path("vehicles/<uuid:pk>/recalls/<uuid:recall_id>/", assets.recall_status, name="recall_status"),
    path("vehicles/<uuid:pk>/report/", assets.asset_report, name="asset_report"),
    path("vehicles/<uuid:pk>/report.pdf", assets.asset_report_pdf, name="asset_report_pdf"),
    # Inspections (DVI)
    path("inspections/", inspections.inspection_list, name="inspection_list"),
    path("inspections/start/", inspections.inspection_start, name="inspection_start"),
    path("inspections/<uuid:pk>/", inspections.inspection_detail, name="inspection_detail"),
    path("inspections/<uuid:pk>/results/<uuid:result_id>/", inspections.result_record,
         name="result_record"),
    path("inspections/<uuid:pk>/complete/", inspections.inspection_complete,
         name="inspection_complete"),
    path("inspections/<uuid:pk>/convert/", inspections.inspection_convert,
         name="inspection_convert"),
    path("inspections/<uuid:pk>/checks/", inspections.inspection_add_check,
         name="inspection_add_check"),
    path("inspections/<uuid:pk>/results/<uuid:result_id>/remove/", inspections.result_remove,
         name="result_remove"),
    path("inspections/<uuid:pk>/abandon/", inspections.inspection_abandon,
         name="inspection_abandon"),
    path("inspections/<uuid:pk>/resume/", inspections.inspection_resume,
         name="inspection_resume"),
    path("inspections/<uuid:pk>/delete/", inspections.inspection_delete,
         name="inspection_delete"),
    path("vehicles/<uuid:pk>/wear/", inspections.wear_chart, name="wear_chart"),
    # Maintenance
    path("due/", maintenance.due_list, name="due_list"),
    path("vehicles/<uuid:pk>/schedule/", maintenance.asset_schedule, name="asset_schedule"),
    path("vehicles/<uuid:pk>/schedule/template/", maintenance.apply_schedule_template,
         name="apply_schedule_template"),
    path("vehicles/<uuid:pk>/schedule/items/", maintenance.service_item_add, name="service_item_add"),
    path("vehicles/<uuid:pk>/schedule/items/<uuid:item_id>/", maintenance.service_item_update,
         name="service_item_update"),
    path("vehicles/<uuid:pk>/schedule/items/<uuid:item_id>/done/", maintenance.service_item_complete,
         name="service_item_complete"),
    path("vehicles/<uuid:pk>/schedule/items/<uuid:item_id>/snooze/", maintenance.service_item_snooze,
         name="service_item_snooze"),
    path("vehicles/<uuid:pk>/components/", maintenance.component_add, name="component_add"),
    path("vehicles/<uuid:pk>/components/<uuid:component_id>/remove/", maintenance.component_remove,
         name="component_remove"),
    # Parts and inventory
    path("parts/", parts.part_list, name="part_list"),
    path("parts/new/", parts.part_create, name="part_create"),
    path("parts/by-code/", parts.part_by_code, name="part_by_code"),
    path("parts/search/", parts.part_search, name="part_search"),
    path("parts/cores/", parts.core_list, name="core_list"),
    path("parts/cores/update/", parts.core_update, name="core_update"),
    path("parts/<uuid:pk>/", parts.part_detail, name="part_detail"),
    path("parts/<uuid:pk>/edit/", parts.part_edit, name="part_edit"),
    path("parts/<uuid:pk>/crossrefs/", parts.crossref_add, name="crossref_add"),
    # A wrong interchange number is worse than a missing one: it makes a scan
    # land on the wrong shelf, confidently.
    path("parts/<uuid:pk>/crossrefs/<uuid:ref_id>/remove/", parts.crossref_remove,
         name="crossref_remove"),
    path("parts/<uuid:pk>/delete/", parts.part_delete, name="part_delete"),
    # A fitment is a claim about a vehicle, and claims turn out to be wrong —
    # so it is editable, and "does not fit" is one of the things it can say.
    path("parts/<uuid:pk>/fitments/new/", parts.fitment_add, name="fitment_add"),
    path("parts/<uuid:pk>/fitments/<uuid:fitment_id>/edit/", parts.fitment_edit,
         name="fitment_edit"),
    path("parts/<uuid:pk>/fitments/<uuid:fitment_id>/delete/", parts.fitment_delete,
         name="fitment_delete"),
    path("parts/<uuid:pk>/stock/", parts.lot_add, name="lot_add"),
    path("parts/<uuid:pk>/stock/<uuid:lot_id>/count/", parts.lot_count, name="lot_count"),
    # Everything a lot knows except how many there are: that is the ledger's,
    # and counting is the way to change it.
    path("parts/<uuid:pk>/stock/<uuid:lot_id>/edit/", parts.lot_edit, name="lot_edit"),
    path("parts/<uuid:pk>/stock/<uuid:lot_id>/delete/", parts.lot_delete, name="lot_delete"),
    # Off the shelf with no job behind it: most of what a home garage has fitted
    # was never a work order here.
    path("parts/<uuid:pk>/use/", parts.part_use, name="part_use"),
    # A kit is a part with other parts recorded inside it. It holds the stock
    # while the box is closed; opening it is what puts the contents on a shelf.
    path("parts/<uuid:pk>/contents/", parts.kit_item_add, name="kit_item_add"),
    path("parts/<uuid:pk>/contents/<uuid:item_id>/remove/", parts.kit_item_remove,
         name="kit_item_remove"),
    path("parts/<uuid:pk>/stock/<uuid:lot_id>/open/", parts.lot_open_kit, name="lot_open_kit"),
    path("parts/<uuid:pk>/stock/<uuid:lot_id>/close/", parts.lot_close_kit, name="lot_close_kit"),
    path("inventory/", parts.inventory, name="inventory"),
    path("inventory/locations/", parts.location_create, name="location_create"),
    # Bins carry printed labels, so a wrong name is expensive and the label
    # survives a rename — it carries the primary key, not the name.
    path("inventory/locations/<uuid:pk>/edit/", parts.location_edit, name="location_edit"),
    path("inventory/locations/<uuid:pk>/delete/", parts.location_delete,
         name="location_delete"),
    # Purchasing
    path("purchases/", purchasing.purchase_list, name="purchase_list"),
    path("purchases/new/", purchasing.purchase_create, name="purchase_create"),
    # A supplier order confirmation becomes a purchase, its lines, and the
    # parts in the catalogue (FR-PUR-1, FR-PART-2).
    path("purchases/import/", purchasing_import.order_import, name="order_import"),
    path("purchases/<uuid:pk>/", purchasing.purchase_detail, name="purchase_detail"),
    # Tax and shipping are not decoration: every lot received against this
    # order is priced from them.
    path("purchases/<uuid:pk>/edit/", purchasing.purchase_edit, name="purchase_edit"),
    path("purchases/<uuid:pk>/lines/", purchasing.purchase_line_add, name="purchase_line_add"),
    path(
        "purchases/<uuid:pk>/lines/<uuid:line_id>/receive/",
        purchasing.purchase_line_receive,
        name="purchase_line_receive",
    ),
    # Correctable only while nothing has been received: the receipt made stock
    # at this price, and changing it underneath would leave lots costed at a
    # number the order no longer states.
    path(
        "purchases/<uuid:pk>/lines/<uuid:line_id>/edit/",
        purchasing.purchase_line_edit,
        name="purchase_line_edit",
    ),
    path(
        "purchases/<uuid:pk>/lines/<uuid:line_id>/delete/",
        purchasing.purchase_line_delete,
        name="purchase_line_delete",
    ),
    # Receiving is one tap on a screen full of lines, so it needs an undo.
    path(
        "purchases/<uuid:pk>/lines/<uuid:line_id>/unreceive/",
        purchasing.purchase_line_unreceive,
        name="purchase_line_unreceive",
    ),
    path("purchases/<uuid:pk>/delete/", purchasing.purchase_delete, name="purchase_delete"),
    path("purchases/<uuid:pk>/receipts/", purchasing.purchase_receipt_upload, name="purchase_receipt_upload"),
    path("vendors/", purchasing.vendor_list, name="vendor_list"),
    path("vendors/new/", purchasing.vendor_create, name="vendor_create"),
    path("vendors/<uuid:pk>/edit/", purchasing.vendor_edit, name="vendor_edit"),
    path("vendors/<uuid:pk>/delete/", purchasing.vendor_delete, name="vendor_delete"),
    # Reports
    path("reports/", core.reports, name="reports"),
    path("reports/export/<str:kind>.csv", core.export_csv, name="export_csv"),
    path("vehicles/<uuid:pk>/costs/", core.asset_costs, name="asset_costs"),
    # People
    path("people/", people.person_list, name="person_list"),
    path("people/new/", people.person_create, name="person_create"),
    path("people/<uuid:pk>/", people.person_detail, name="person_detail"),
    path("people/<uuid:pk>/edit/", people.person_edit, name="person_edit"),
    path("people/<uuid:pk>/delete/", people.person_delete, name="person_delete"),
    # API + admin
    path("api/v1/", api.urls),
    path("admin/", admin.site.urls),
]

# `runserver` convenience only, and it has to stay that way: MEDIA_ROOT served
# straight off the filesystem is every photo in the shop readable by anyone who
# guesses a path, with no login. Media is served by `mediafiles.views`, which
# checks. Widening this condition would not even work — `static()` returns
# nothing unless DEBUG — which is exactly why it is worth being explicit about
# what it is for.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
