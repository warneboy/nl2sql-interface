// ===============================
// NORMAL LOGIN HANDLER
// ===============================

document.addEventListener('DOMContentLoaded', function() {
    
    // Setup login form
    const loginForm = document.getElementById('login-form');
    if (loginForm) {
        loginForm.addEventListener('submit', async function(e) {
            e.preventDefault();
            const email = document.getElementById('login-email').value;
            const password = document.getElementById('login-password').value;
            
            const msg = document.getElementById('auth-message');
            msg.textContent = 'Signing in...';
            msg.className = 'info';
            msg.style.display = 'block';
            
            try {
                const response = await fetch('/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, password })
                });
                const data = await response.json();
                if (response.ok) {
                    localStorage.setItem('authToken', data.token);
                    localStorage.setItem('user', JSON.stringify(data.user));
                    window.location.href = '/dashboard?token=' + data.token;
                } else {
                    msg.textContent = data.message || 'Login failed';
                    msg.className = 'error';
                    msg.style.display = 'block';
                }
            } catch (error) {
                console.error('Login error:', error);
                msg.textContent = 'Unable to connect to server';
                msg.className = 'error';
                msg.style.display = 'block';
            }
        });
    }
    
    // Setup signup form
    const signupForm = document.getElementById('signup-form');
    if (signupForm) {
        signupForm.addEventListener('submit', async function(e) {
            e.preventDefault();
            const name = document.getElementById('signup-name').value;
            const email = document.getElementById('signup-email').value;
            const password = document.getElementById('signup-password').value;
            const confirm = document.getElementById('signup-confirm').value;
            
            if (password !== confirm) {
                const msg = document.getElementById('signup-message');
                msg.textContent = 'Passwords do not match';
                msg.className = 'error';
                msg.style.display = 'block';
                return;
            }
            
            if (password.length < 8) {
                const msg = document.getElementById('signup-message');
                msg.textContent = 'Password must be at least 8 characters';
                msg.className = 'error';
                msg.style.display = 'block';
                return;
            }
            
            try {
                const response = await fetch('/signup', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ full_name: name, email, password })
                });
                const data = await response.json();
                if (response.ok) {
                    const msg = document.getElementById('signup-message');
                    msg.textContent = 'Account created! Please login.';
                    msg.className = 'success';
                    msg.style.display = 'block';
                    setTimeout(() => {
                        document.querySelector('.signup-card').style.display = 'none';
                        document.querySelector('.auth-card').style.display = 'block';
                        document.getElementById('login-email').value = email;
                        document.getElementById('login-password').value = '';
                    }, 1500);
                } else {
                    const msg = document.getElementById('signup-message');
                    msg.textContent = data.message || 'Signup failed';
                    msg.className = 'error';
                    msg.style.display = 'block';
                }
            } catch (error) {
                console.error('Signup error:', error);
                const msg = document.getElementById('signup-message');
                msg.textContent = 'Unable to connect to server';
                msg.className = 'error';
                msg.style.display = 'block';
            }
        });
    }
    
    // Switch between login and signup
    const showSignup = document.getElementById('show-signup');
    if (showSignup) {
        showSignup.addEventListener('click', function() {
            document.querySelector('.auth-card').style.display = 'none';
            document.querySelector('.signup-card').style.display = 'block';
            clearMessages();
        });
    }
    
    const showLogin = document.getElementById('show-login');
    if (showLogin) {
        showLogin.addEventListener('click', function() {
            document.querySelector('.signup-card').style.display = 'none';
            document.querySelector('.auth-card').style.display = 'block';
            clearMessages();
        });
    }
});

function clearMessages() {
    const authMsg = document.getElementById('auth-message');
    authMsg.className = '';
    authMsg.textContent = '';
    authMsg.style.display = 'none';
    
    const signupMsg = document.getElementById('signup-message');
    signupMsg.className = '';
    signupMsg.textContent = '';
    signupMsg.style.display = 'none';
}

// ===============================
// GOOGLE LOGIN
// ===============================

let googleInitialized = false;
let googleClientId = null;

// Show error/success message in the auth card
function showGoogleError(message) {
    const msg = document.getElementById('auth-message');
    if (msg) {
        msg.textContent = message;
        msg.className = 'error';
        msg.style.display = 'block';
    }
}

// Handle Google credential received from Google Identity Services
function handleGoogleCredential(response) {
    if (!response || !response.credential) {
        showGoogleError('Google login failed. Please try again.');
        return;
    }

    const msg = document.getElementById('auth-message');
    msg.textContent = 'Signing in with Google...';
    msg.className = 'info';
    msg.style.display = 'block';

    fetch('/auth/google', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credential: response.credential })
    })
    .then(res => res.json())
    .then(data => {
        if (data.success) {
            localStorage.setItem('authToken', data.token);
            localStorage.setItem('user', JSON.stringify(data.user));
            window.location.href = '/dashboard?token=' + data.token;
        } else {
            showGoogleError(data.message || 'Google login failed');
        }
    })
    .catch(err => {
        console.error('Google auth error:', err);
        showGoogleError('Unable to reach the server. Please try again.');
    });
}

// Initialize and render the Google Sign-In button
function initGoogleLogin() {
    if (googleInitialized) return;

    const loginContainer = document.getElementById('google-login');
    const signupContainer = document.getElementById('google-signup');
    if (!loginContainer && !signupContainer) return;

    // Load the Google client ID from the server (single source of truth)
    if (!googleClientId) {
        fetch('/google-client-id')
            .then(res => res.json())
            .then(data => {
                googleClientId = data.client_id;
                if (!googleClientId) {
                    showGoogleError('Google login is not configured on this server.');
                    return;
                }
                renderGoogleButtons(loginContainer, signupContainer);
            })
            .catch(() => {
                showGoogleError('Could not load Google login configuration.');
            });
        return;
    }

    renderGoogleButtons(loginContainer, signupContainer);
}

function renderGoogleButtons(loginContainer, signupContainer) {
    if (typeof google === 'undefined' || !google.accounts) {
        // Google API script has not loaded yet; retry on window load.
        return;
    }

    googleInitialized = true;

    try {
        google.accounts.id.initialize({
            client_id: googleClientId,
            callback: handleGoogleCredential,
            auto_select: false,
            cancel_on_tap_outside: true
        });

        if (loginContainer) {
            google.accounts.id.renderButton(loginContainer, {
                type: 'standard',
                theme: 'outline',
                size: 'large',
                text: 'continue_with',
                shape: 'rectangular',
                logo_alignment: 'left',
                width: 350
            });
        }

        if (signupContainer) {
            google.accounts.id.renderButton(signupContainer, {
                type: 'standard',
                theme: 'outline',
                size: 'large',
                text: 'signup_with',
                shape: 'rectangular',
                logo_alignment: 'left',
                width: 350
            });
        }
    } catch (error) {
        console.error('Error initializing Google Sign-In:', error);
        googleInitialized = false;
        showGoogleError('Google Sign-In could not be loaded.');
    }
}

// Load Google client ID and initialize once the API is available
document.addEventListener('DOMContentLoaded', function() {
    initGoogleLogin();
});

window.addEventListener('load', function() {
    initGoogleLogin();
});

// Fallback: show a notice if the Google API script fails to load
setTimeout(function() {
    if (!googleInitialized && typeof google === 'undefined') {
        showGoogleError('Google Sign-In could not be loaded. Check your internet connection.');
    }
}, 8000);

// ===============================
// CHECK LOGIN STATUS
// ===============================

// Check if user is already logged in
const token = localStorage.getItem('authToken');
if (token) {
    fetch('/verify-token', {
        headers: { 'Authorization': `Bearer ${token}` }
    })
    .then(res => res.json())
    .then(data => {
        if (data.valid) {
            window.location.href = '/dashboard?token=' + token;
        } else {
            localStorage.removeItem('authToken');
            localStorage.removeItem('user');
        }
    })
    .catch(() => {});
}