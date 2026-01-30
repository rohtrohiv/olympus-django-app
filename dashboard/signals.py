import threading
import time
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django_auth_adfs import signals as adfs_signals

_thread_local = threading.local()
_SESSION_CLAIMS_KEY = "sso_claims"
_SESSION_ADFS_KEY = "sso_adfs"


def _set_local(name, value):
	setattr(_thread_local, name, value)


def _pop_local(name):
	if hasattr(_thread_local, name):
		value = getattr(_thread_local, name)
		delattr(_thread_local, name)
		return value
	return None


@receiver(adfs_signals.post_authenticate)
def _cache_adfs_claims(sender, user, claims, adfs_response, **kwargs):
	"""Cache SSO claims until the session is available."""
	_set_local("claims", claims or {})
	_set_local("adfs_response", adfs_response or {})


@receiver(user_logged_in)
def _store_claims_in_session(sender, request, user, **kwargs):
	"""Persist cached claims in the authenticated session."""
	claims = _pop_local("claims") or {}
	request.session[_SESSION_CLAIMS_KEY] = claims
	adfs_response = _pop_local("adfs_response") or {}
	if adfs_response:
		token_snapshot = {k: adfs_response.get(k) for k in ("access_token", "refresh_token", "token_type", "expires_in", "ext_expires_in") if adfs_response.get(k)}
		if token_snapshot:
			token_snapshot["obtained_at"] = int(time.time())
			request.session[_SESSION_ADFS_KEY] = token_snapshot
