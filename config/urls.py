from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from homeautoshop.api.urls import api
from homeautoshop.assets import views as assets
from homeautoshop.core import views as core
from homeautoshop.core import views_integrations as integrations
from homeautoshop.core import views_settings as instance_settings
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
        auth_views.LoginView.as_view(template_name="auth/login.html", redirect_authenticated_user=True),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
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
    path("vehicles/<uuid:pk>/decode/", assets.vin_decode, name="vin_decode"),
    path("vehicles/<uuid:pk>/readings/", assets.reading_create, name="reading_create"),
    path("vehicles/<uuid:pk>/photos/", assets.photo_upload, name="asset_photo_upload"),
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
    path("work-orders/<uuid:pk>/photos/", work.work_order_photo, name="work_order_photo"),
    path("work-orders/<uuid:pk>/parts/", work.part_use, name="work_order_part_use"),
    path("work-orders/<uuid:pk>/time/", work.time_add, name="work_order_time_add"),
    path("work-orders/<uuid:pk>/items/<uuid:item_id>/tools/", work.job_item_tool_add,
         name="job_item_tool_add"),
    path("work-orders/<uuid:pk>/tools/<uuid:reference_id>/remove/", work.job_item_tool_remove,
         name="job_item_tool_remove"),
    # Tool lookup for a job item, so nobody has to remember a WrenchLedger id.
    path("tools/search/", work.tool_search, name="tool_search"),
    path("work-orders/<uuid:pk>/expenses/", purchasing.expense_add, name="work_order_expense_add"),
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
    path("vehicles/<uuid:pk>/report.pdf", assets.asset_report, name="asset_report"),
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
    path("parts/<uuid:pk>/", parts.part_detail, name="part_detail"),
    path("parts/<uuid:pk>/edit/", parts.part_edit, name="part_edit"),
    path("parts/<uuid:pk>/crossrefs/", parts.crossref_add, name="crossref_add"),
    path("parts/<uuid:pk>/stock/", parts.lot_add, name="lot_add"),
    path("parts/<uuid:pk>/stock/<uuid:lot_id>/count/", parts.lot_count, name="lot_count"),
    path("inventory/", parts.inventory, name="inventory"),
    path("inventory/locations/", parts.location_create, name="location_create"),
    path("inventory/cores/<uuid:usage_id>/returned/", parts.core_returned, name="core_returned"),
    # Purchasing
    path("purchases/", purchasing.purchase_list, name="purchase_list"),
    path("purchases/new/", purchasing.purchase_create, name="purchase_create"),
    # A supplier order confirmation becomes a purchase, its lines, and the
    # parts in the catalogue (FR-PUR-1, FR-PART-2).
    path("purchases/import/", purchasing_import.order_import, name="order_import"),
    path("purchases/<uuid:pk>/", purchasing.purchase_detail, name="purchase_detail"),
    path("purchases/<uuid:pk>/lines/", purchasing.purchase_line_add, name="purchase_line_add"),
    path(
        "purchases/<uuid:pk>/lines/<uuid:line_id>/receive/",
        purchasing.purchase_line_receive,
        name="purchase_line_receive",
    ),
    path("purchases/<uuid:pk>/receipts/", purchasing.purchase_receipt_upload, name="purchase_receipt_upload"),
    path("vendors/", purchasing.vendor_list, name="vendor_list"),
    path("vendors/new/", purchasing.vendor_create, name="vendor_create"),
    # Reports
    path("reports/", core.reports, name="reports"),
    path("reports/export/<str:kind>.csv", core.export_csv, name="export_csv"),
    path("vehicles/<uuid:pk>/costs/", core.asset_costs, name="asset_costs"),
    # People
    path("people/", people.person_list, name="person_list"),
    path("people/new/", people.person_create, name="person_create"),
    path("people/<uuid:pk>/", people.person_detail, name="person_detail"),
    path("people/<uuid:pk>/edit/", people.person_edit, name="person_edit"),
    # API + admin
    path("api/v1/", api.urls),
    path("admin/", admin.site.urls),
]

if settings.DEBUG or settings.STORAGE_DRIVER == "filesystem":
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
