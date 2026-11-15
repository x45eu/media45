from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path
from catalog import views, mfa_views, passkey_views, pwa
from catalog.login_guard import ProtectedAuthenticationForm, ProtectedAdminAuthenticationForm

admin.site.login_form = ProtectedAdminAuthenticationForm

urlpatterns = [
    path("manifest.webmanifest", pwa.manifest, name="pwa_manifest"),
    path("service-worker.js", pwa.service_worker, name="pwa_service_worker"),
    path("", views.index, name="index"),
    path("quellen/", views.credits, name="credits"),
    path("requests/new/", views.new_request, name="new_request"),
    path("requests/mine/", views.my_requests, name="my_requests"),
    path("requests/mine/<int:pk>/delete/", views.delete_my_request, name="delete_my_request"),
    path("media/<int:pk>/", views.media_detail, name="media_detail"),
    path("media/<int:pk>/cover/", views.cover_image, name="media_cover"),
    path("review/", views.review_matches, name="review_matches"),
    path("review/group/<int:pk>/", views.review_group, name="review_group"),
    path("login/", auth_views.LoginView.as_view(template_name="catalog/login.html", authentication_form=ProtectedAuthenticationForm), name="login"),
    path("account/mfa/", mfa_views.verify, name="mfa_verify"),
    path("account/mfa/setup/", mfa_views.setup, name="mfa_setup"),
    path("account/mfa/settings/", mfa_views.settings, name="mfa_settings"),
    path("account/mfa/recovery/", mfa_views.recovery, name="mfa_recovery"),
    path("account/passkeys/", passkey_views.manage, name="passkeys_manage"),
    path("passkey-login/options/", passkey_views.passwordless_options, name="passwordless_options"),
    path("passkey-login/finish/", passkey_views.passwordless_finish, name="passwordless_finish"),
    path("account/passkeys/register/options/", passkey_views.register_options, name="passkeys_register_options"),
    path("account/passkeys/register/finish/", passkey_views.register_finish, name="passkeys_register_finish"),
    path("account/passkeys/auth/options/", passkey_views.auth_options, name="passkeys_auth_options"),
    path("account/passkeys/auth/finish/", passkey_views.auth_finish, name="passkeys_auth_finish"),
    path("account/passkeys/<int:pk>/remove/", passkey_views.remove, name="passkeys_remove"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("account/password/", auth_views.PasswordChangeView.as_view(template_name="catalog/password_change.html", success_url="/account/password/done/"), name="password_change"),
    path("account/password/done/", auth_views.PasswordChangeDoneView.as_view(template_name="catalog/password_change_done.html"), name="password_change_done"),
    path("admin/", admin.site.urls),
]
