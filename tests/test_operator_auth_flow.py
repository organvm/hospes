"""Tests for hospes.operator auth flow."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from hospes.operator import create_operator_app


class TestOperatorAuthFlow:
    """Tests for operator authentication flow."""

    def setup_method(self):
        """Create a test app for each test."""
        self.app = create_operator_app(
            db_path=":memory:",
            auth_token="synthetic-test-token-123",  # allow-secret: inert test fixture
            actor_id="test_actor",
            role="producer",
            tenant_id="test_tenant",
            session_secret="synthetic-session-secret-123",  # allow-secret: inert test fixture
            enable_raw_v1=False,
        )
        self.client = TestClient(self.app)

    def test_login_redirects_when_session_exists(self):
        """Test login redirects to /operator/ when session exists."""
        with self.client as client:
            client.cookies.set(
                "hospes_operator_session", "synthetic-session-secret-123"
            )
            response = client.get("/operator/login", follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/operator/"

    def test_login_serves_login_page_when_no_session(self):
        """Test login serves login page when no session."""
        with self.client as client:
            response = client.get("/operator/login")
            assert response.status_code == 200
            assert "login" in response.text.lower()

    def test_establish_session_valid_token_sets_httponly_cookie(self):
        """Test establishing session with valid token sets HttpOnly cookie."""
        with self.client as client:
            response = client.post(
                "/operator/session",
                data={"token": "synthetic-test-token-123"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert "hospes_operator_session" in client.cookies
            cookie_header = response.headers.get("set-cookie", "")
            assert "httponly" in cookie_header.lower()
            assert "samesite=strict" in cookie_header.lower()

    def test_establish_session_invalid_token_returns_401(self):
        """Test establishing session with invalid token returns 401."""
        with self.client as client:
            response = client.post(
                "/operator/session",
                data={"token": "wrong-token"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert response.status_code == 401

    def test_establish_session_oversize_body_returns_413(self):
        """Test establishing session with oversize body returns 413."""
        with self.client as client:
            # Create a body larger than 8KB
            large_body = "token=" + "x" * 9000
            response = client.post(
                "/operator/session",
                content=large_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert response.status_code == 413

    def test_logout_clears_cookie_redirects_login(self):
        """Test logout clears cookie and redirects to login."""
        with self.client as client:
            login = client.post(
                "/operator/session",
                data={"token": "synthetic-test-token-123"},
                follow_redirects=False,
            )
            assert login.status_code == 303
            csrf = client.cookies.get("hospes_csrf")
            response = client.post(
                "/operator/logout", data={"csrf": csrf}, follow_redirects=False
            )
            assert response.status_code in (307, 303)
            assert response.headers["location"] == "/operator/login"
            # Check cookie is cleared
            cookie_header = response.headers.get("set-cookie", "")
            assert "hospes_operator_session=" in cookie_header
            assert "hospes_csrf=" in cookie_header
            assert "Max-Age=0" in cookie_header or "Expires=" in cookie_header

    def test_root_redirects_to_operator(self):
        """Test root URL redirects to /operator/."""
        with self.client as client:
            response = client.get("/", follow_redirects=False)
            assert response.status_code == 307
            assert response.headers["location"] == "/operator/"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
