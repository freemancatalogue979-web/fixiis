"""Shared WebAuthn/passkey blocking for both browser backends.

The Playwright path (browser_manager._apply_webauthn_disable) proved this
two-layer approach on real sites: a JS override installed on every new
document plus a CDP virtual authenticator so WebAuthn requests never
reach the OS-level UI (Windows Hello / Microsoft passkey popups). Sites
then fall back to password login automatically. Extracted into a module
so the SeleniumBase backend (CDP passthrough) applies the exact same
block instead of drifting.
"""

WEBAUTHN_BLOCK_JS = r"""            (function() {
                'use strict';
                
                // Disable navigator.credentials (WebAuthn API)
                // This prevents sites from initiating WebAuthn/passkey flows
                if (navigator.credentials) {
                    // Override create to reject with NotAllowedError (simulates user cancellation)
                    navigator.credentials.create = (options) => {
                        console.log('[WebAuthn Block] credentials.create called - blocking');
                        return Promise.reject(new DOMException("WebAuthn disabled", "NotAllowedError"));
                    };
                    
                    // Override get to reject with NotAllowedError
                    navigator.credentials.get = (options) => {
                        console.log('[WebAuthn Block] credentials.get called - blocking');
                        return Promise.reject(new DOMException("WebAuthn disabled", "NotAllowedError"));
                    };
                    
                    // Prevent redefined
                    Object.defineProperty(navigator.credentials, 'create', {
                        get: () => () => Promise.reject(new DOMException("WebAuthn disabled", "NotAllowedError")),
                        configurable: false
                    });
                    Object.defineProperty(navigator.credentials, 'get', {
                        get: () => () => Promise.reject(new DOMException("WebAuthn disabled", "NotAllowedError")),
                        configurable: false
                    });
                }
                
                // Also block the underlying WebAuthn API if available
                if (window.PublicKeyCredential) {
                    // Make PublicKeyCredential always return false for isUserVerifyingPlatformAuthenticatorAvailable
                    const origIsUVPA = PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable;
                    if (typeof origIsUVPA === 'function') {
                        PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable = function() {
                            console.log('[WebAuthn Block] isUserVerifyingPlatformAuthenticatorAvailable called - returning false');
                            return Promise.resolve(false);
                        };
                    }
                    
                    // Block platform authenticator detection
                    const origIsConditional = PublicKeyCredential.isConditionalMediationAvailable;
                    if (typeof origIsConditional === 'function') {
                        PublicKeyCredential.isConditionalMediationAvailable = function() {
                            console.log('[WebAuthn Block] isConditionalMediationAvailable called - returning false');
                            return Promise.resolve(false);
                        };
                    }
                }
                
                console.log('[WebAuthn Block] WebAuthn API blocking applied successfully');
            })();
            """

# CDP virtual-authenticator options that make Chrome believe an
# authenticator exists but never surface the OS prompt.
VIRTUAL_AUTHENTICATOR_OPTIONS = {
    "protocol": "ctap2",
    "transport": "internal",
    "hasResidentKey": True,
    "hasUserVerification": True,
    "isUserVerified": True,
    "automaticPresenceSimulation": True,
}
