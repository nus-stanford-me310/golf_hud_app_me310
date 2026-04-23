// ============ STATE ============
const CLUBS = [
    "Driver", "3 Wood", "5 Wood", "Hybrid",
    "3 Iron", "4 Iron", "5 Iron", "6 Iron", "7 Iron",
    "8 Iron", "9 Iron", "PW", "SW", "LW",
];

const TOKEN_KEY = "golfhud_token";
let currentUser = null;

const getToken = () => localStorage.getItem(TOKEN_KEY);
const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
const clearToken = () => localStorage.removeItem(TOKEN_KEY);

// ============ API ============
async function api(path, { method = "GET", body, auth = true } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (auth) {
        const token = getToken();
        if (token) headers.Authorization = `Bearer ${token}`;
    }
    const res = await fetch(path, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        try {
            const j = await res.json();
            if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
        } catch (_) {}
        throw new Error(msg);
    }
    return res.status === 204 ? null : res.json();
}

// ============ ROUTER ============
function route() {
    const hash = window.location.hash || "#/login";
    const token = getToken();

    if (hash === "#/signup") return renderSignup();
    if (hash === "#/profile") {
        if (!token) return (window.location.hash = "#/login");
        return renderProfile();
    }
    // default → login (or profile if already authed)
    if (token) return (window.location.hash = "#/profile");
    renderLogin();
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);

function mount(templateId) {
    const app = document.getElementById("app");
    app.innerHTML = "";
    const tpl = document.getElementById(templateId);
    app.appendChild(tpl.content.cloneNode(true));
}

function showError(id, msg) {
    const el = document.getElementById(id);
    el.textContent = msg;
    el.hidden = false;
}

function hideError(id) {
    const el = document.getElementById(id);
    el.hidden = true;
}

// ============ LOGIN ============
function renderLogin() {
    mount("tpl-login");
    document.getElementById("login-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        hideError("login-error");
        const fd = new FormData(e.target);
        const btn = e.target.querySelector("button[type=submit]");
        btn.disabled = true;
        btn.textContent = "Logging in…";
        try {
            const resp = await api("/auth/login", {
                method: "POST",
                auth: false,
                body: {
                    email: fd.get("email"),
                    password: fd.get("password"),
                },
            });
            setToken(resp.access_token);
            window.location.hash = "#/profile";
        } catch (err) {
            showError("login-error", err.message);
        } finally {
            btn.disabled = false;
            btn.textContent = "Log in";
        }
    });
}

// ============ SIGNUP ============
function renderSignup() {
    mount("tpl-signup");

    // build club inputs
    const grid = document.getElementById("clubs-grid");
    grid.innerHTML = CLUBS.map(club => `
        <div class="club-input">
            <label>${club}</label>
            <div class="club-input-wrap">
                <input type="number" min="0" max="400" step="1" name="club::${club}" placeholder="—" />
            </div>
        </div>
    `).join("");

    let page = 1;
    const updatePage = () => {
        document.querySelectorAll("fieldset[data-page]").forEach(fs => {
            fs.hidden = Number(fs.dataset.page) !== page;
        });
        document.querySelectorAll(".step").forEach(s => {
            s.classList.toggle("active", Number(s.dataset.step) === page);
        });
        document.getElementById("signup-back").hidden = page === 1;
        document.getElementById("signup-next").hidden = page === 3;
        document.getElementById("signup-submit").hidden = page !== 3;
    };
    updatePage();

    document.getElementById("signup-back").addEventListener("click", () => {
        page = Math.max(1, page - 1);
        updatePage();
    });

    document.getElementById("signup-next").addEventListener("click", () => {
        hideError("signup-error");
        // basic validation per page
        if (page === 1) {
            const form = document.getElementById("signup-form");
            const { name, email, password } = Object.fromEntries(new FormData(form));
            if (!name || !email || !password) {
                return showError("signup-error", "Fill all fields");
            }
            if (password.length < 6) {
                return showError("signup-error", "Password must be at least 6 characters");
            }
        }
        page = Math.min(3, page + 1);
        updatePage();
    });

    document.getElementById("signup-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        hideError("signup-error");

        const fd = new FormData(e.target);
        const obj = Object.fromEntries(fd);

        const club_distances = [];
        for (const club of CLUBS) {
            const val = obj[`club::${club}`];
            if (val !== "" && val != null) {
                const n = Number(val);
                if (!isNaN(n) && n > 0) club_distances.push({ club, avg_yardage: n });
            }
        }

        const payload = {
            email: obj.email,
            password: obj.password,
            name: obj.name,
            handicap: obj.handicap ? Number(obj.handicap) : null,
            skill_level: obj.skill_level || null,
            club_distances,
        };

        const btn = document.getElementById("signup-submit");
        btn.disabled = true;
        btn.textContent = "Creating…";
        try {
            const resp = await api("/auth/signup", {
                method: "POST",
                auth: false,
                body: payload,
            });
            setToken(resp.access_token);
            window.location.hash = "#/profile";
        } catch (err) {
            showError("signup-error", err.message);
        } finally {
            btn.disabled = false;
            btn.textContent = "Create account";
        }
    });
}

// ============ PROFILE ============
async function renderProfile() {
    mount("tpl-profile");

    document.getElementById("logout-btn").addEventListener("click", () => {
        clearToken();
        currentUser = null;
        window.location.hash = "#/login";
    });

    try {
        const user = await api("/users/me");
        currentUser = user;
        document.getElementById("profile-name").textContent = user.name;
        document.getElementById("profile-email").textContent = user.email;
        document.getElementById("profile-handicap").textContent =
            user.handicap != null ? user.handicap : "—";
        document.getElementById("profile-skill").textContent =
            user.skill_level ? user.skill_level.charAt(0).toUpperCase() + user.skill_level.slice(1) : "—";
        document.getElementById("profile-uid").textContent = user.user_id;

        renderClubsView(user.club_distances);
        bindClubsEdit(user.club_distances);
    } catch (err) {
        if (String(err.message).includes("401")) {
            clearToken();
            return (window.location.hash = "#/login");
        }
        showError("profile-error", err.message);
    }
}

function renderClubsView(clubs) {
    const view = document.getElementById("clubs-view");
    if (!clubs || clubs.length === 0) {
        view.innerHTML = '<p class="empty">No clubs recorded yet. Tap Edit to add them.</p>';
        return;
    }
    view.innerHTML = clubs
        .map(c => `<div class="club-row"><span>${c.club}</span><strong>${Math.round(c.avg_yardage)} yd</strong></div>`)
        .join("");
}

function bindClubsEdit(initialClubs) {
    const view = document.getElementById("clubs-view");
    const form = document.getElementById("clubs-form");
    const grid = document.getElementById("clubs-edit-grid");
    const editBtn = document.getElementById("clubs-edit");
    const cancelBtn = document.getElementById("clubs-cancel");

    const byClub = Object.fromEntries((initialClubs || []).map(c => [c.club, c.avg_yardage]));

    const buildGrid = (values) => {
        grid.innerHTML = CLUBS.map(club => {
            const v = values[club] != null ? Math.round(values[club]) : "";
            return `
                <div class="club-input">
                    <label>${club}</label>
                    <div class="club-input-wrap">
                        <input type="number" min="0" max="400" step="1" name="club::${club}" value="${v}" />
                    </div>
                </div>
            `;
        }).join("");
    };

    editBtn.addEventListener("click", () => {
        buildGrid(byClub);
        view.hidden = true;
        form.hidden = false;
        editBtn.hidden = true;
    });

    cancelBtn.addEventListener("click", () => {
        view.hidden = false;
        form.hidden = true;
        editBtn.hidden = false;
    });

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        hideError("profile-error");

        const fd = new FormData(form);
        const obj = Object.fromEntries(fd);
        const club_distances = [];
        for (const club of CLUBS) {
            const val = obj[`club::${club}`];
            if (val !== "" && val != null) {
                const n = Number(val);
                if (!isNaN(n) && n > 0) club_distances.push({ club, avg_yardage: n });
            }
        }

        const btn = form.querySelector("button[type=submit]");
        btn.disabled = true;
        btn.textContent = "Saving…";
        try {
            const clubs = await api("/users/me/clubs", {
                method: "PUT",
                body: { club_distances },
            });
            renderClubsView(clubs);
            Object.keys(byClub).forEach(k => delete byClub[k]);
            clubs.forEach(c => (byClub[c.club] = c.avg_yardage));
            view.hidden = false;
            form.hidden = true;
            editBtn.hidden = false;
        } catch (err) {
            showError("profile-error", err.message);
        } finally {
            btn.disabled = false;
            btn.textContent = "Save";
        }
    });
}
