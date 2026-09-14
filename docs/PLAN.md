# Exam Proctor — Project Plan

> UI and implementation conventions that complement [SYSTEM_DESIGN.md](./SYSTEM_DESIGN.md).

---

## UI & Iconography

| Rule | Detail |
|------|--------|
| **No emojis in the UI** | Do not use emoji characters (📊, 🚩, 👥, etc.) in templates, navigation, buttons, labels, or dashboard cards. |
| **Use Flaticon icons** | Use icons from [Flaticon](https://www.flaticon.com/) instead of emojis for all visual indicators. |
| **Asset location** | Save downloaded Flaticon assets under `static/icons/flaticon/` (SVG preferred; PNG only when SVG is unavailable). |
| **Markup pattern** | Use `{% include "includes/icon.html" with name="dashboard" size="sm" only %}` — see `templates/includes/icon.html`. |
| **Consistency** | Reuse the same icon set/style (line weight, size, color treatment) across sidebar, dashboard stats, and action buttons. |
| **Licensing** | Keep Flaticon attribution/license notes in `static/icons/flaticon/ATTRIBUTION.md` when required by the icon license. |

When adding or updating navigation (`portal_sidebar.html`), dashboards, or cards, replace any existing emoji with the matching Flaticon asset — do not introduce new emojis.

---

## Typography & emphasis (monochromatic hierarchy)

Design the UI using a **monochromatic text hierarchy**. Do not use colored text (red, orange, green, etc.) to highlight important information.

| Do | Don't |
|----|-------|
| Use **font-weight contrast** (Bold vs. Regular) to establish hierarchy | Use red/orange/green text for alerts, warnings, or success |
| Use **structural containers** (cards, borders, spacing) to group related content | Rely on `color: var(--color-danger)` or similar on body copy |
| House important warnings in a **neutral light-gray card** with a distinct **icon prefix** and a **bold header** | Use colored typography alone to signify urgency |

**Warning / alert pattern:**

```html
<div class="portal-notice-card">
  {% include "includes/icon.html" with name="warning" size="sm" only %}
  <div>
    <strong class="portal-notice-title">Action required</strong>
    <p class="portal-notice-body">Description in regular weight, neutral text color.</p>
  </div>
</div>
```

Urgency comes from **layout, weight, and icon** — not from colored type. Reserve color for interactive states (buttons, badges with backgrounds) where the design system already defines it; avoid colored inline text in paragraphs, labels, or stat meta lines.

---

## Portal navigation

- Single shell: `templates/layouts/portal.html`
- Sidebar: `templates/includes/portal_sidebar.html`
- Tab content: `templates/partials/portal/` via `render_portal()` in `apps/accounts/portal.py`
- See `.cursor/rules/confirm-todo-list.mdc` for tab-navigation workflow
- Auth pages (login, register, pending-approval, ID review) render their own document and must never be swapped into `#portal-main`. `HtmxAuthRedirectMiddleware` answers HTMX requests bounced to those pages with `HX-Redirect` so the browser navigates fully; `portal.js` refuses the swap as a fallback.
- `render_portal()` sends `no-store`, and `CSRF_FAILURE_VIEW` (`accounts.views.csrf_failure`) redirects stale-token POSTs to login / the dashboard / the form instead of Django's 403 page. Both exist because a portal page left open across a logout keeps a CSRF token that has since rotated.

---

## Before implementing UI work

1. Create a reviewable todo list (see `.cursor/rules/confirm-todo-list.mdc`)
2. Check for existing shared components — extend, do not duplicate
3. Confirm icon assets exist in `static/icons/flaticon/` before wiring new nav items or cards
