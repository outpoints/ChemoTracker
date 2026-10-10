"""SVG icon paths.

Adapted from Lucide (https://lucide.dev, ISC License; see THIRD_PARTY_NOTICES/lucide-icons.txt).
"ring" is an original drawing in the same style. Every icon is drawn on a 24x24
grid with a 2px stroke, so they all sit at the same visual weight.
"""

from __future__ import annotations

from markupsafe import Markup

PATHS: dict[str, str] = {
    "sunrise": '<path d="M12 2v8"/> <path d="m4.93 10.93 1.41 1.41"/> <path d="M2 18h2"/> <path d="M20 18h2"/> <path d="m19.07 10.93-1.41 1.41"/> <path d="M22 22H2"/> <path d="m8 6 4-4 4 4"/> <path d="M16 18a4 4 0 0 0-8 0"/>',
    "sun": '<circle cx="12" cy="12" r="4"/> <path d="M12 2v2"/> <path d="M12 20v2"/> <path d="m4.93 4.93 1.41 1.41"/> <path d="m17.66 17.66 1.41 1.41"/> <path d="M2 12h2"/> <path d="M20 12h2"/> <path d="m6.34 17.66-1.41 1.41"/> <path d="m19.07 4.93-1.41 1.41"/>',
    "sunset": '<path d="M12 10V2"/> <path d="m4.93 10.93 1.41 1.41"/> <path d="M2 18h2"/> <path d="M20 18h2"/> <path d="m19.07 10.93-1.41 1.41"/> <path d="M22 22H2"/> <path d="m16 6-4 4-4-4"/> <path d="M16 18a4 4 0 0 0-8 0"/>',
    "moon": '<path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401"/>',
    "pill": '<path d="m10.5 20.5 10-10a4.95 4.95 0 1 0-7-7l-10 10a4.95 4.95 0 1 0 7 7Z"/> <path d="m8.5 8.5 7 7"/>',
    "pill-bottle": '<path d="M18 11h-4a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h4"/> <path d="M6 7v13a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7"/> <rect width="16" height="5" x="4" y="2" rx="1"/>',
    "syringe": '<path d="m18 2 4 4"/> <path d="m17 7 3-3"/> <path d="M19 9 8.7 19.3c-1 1-2.5 1-3.4 0l-.6-.6c-1-1-1-2.5 0-3.4L15 5"/> <path d="m9 11 4 4"/> <path d="m5 19-3 3"/> <path d="m14 4 6 6"/>',
    "hospital": '<path d="M12 7v4"/> <path d="M14 21v-3a2 2 0 0 0-4 0v3"/> <path d="M14 9h-4"/> <path d="M18 11h2a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2h2"/> <path d="M18 21V5a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18"/> <path d="m6 6 12 12"/>',
    "clock": '<circle cx="12" cy="12" r="10"/> <path d="M12 6v6l4 2"/>',
    "phone": '<path d="M13.832 16.568a1 1 0 0 0 1.213-.303l.355-.465A2 2 0 0 1 17 15h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2A18 18 0 0 1 2 4a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v3a2 2 0 0 1-.8 1.6l-.468.351a1 1 0 0 0-.292 1.233 14 14 0 0 0 6.392 6.384"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "plus": '<path d="M5 12h14"/> <path d="M12 5v14"/>',
    "minus": '<path d="M5 12h14"/>',
    "triangle-alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/> <path d="M12 9v4"/> <path d="M12 17h.01"/>',
    "thermometer": '<path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/>',
    "droplet": '<path d="M12 22a7 7 0 0 0 7-7c0-2-1-3.9-3-5.5s-3.5-4-4-6.5c-.5 2.5-2 4.9-4 6.5C6 11.1 5 13 5 15a7 7 0 0 0 7 7z"/>',
    "droplets": '<path d="M7 16.3c2.2 0 4-1.83 4-4.05 0-1.16-.57-2.26-1.71-3.19S7.29 6.75 7 5.3c-.29 1.45-1.14 2.84-2.29 3.76S3 11.1 3 12.25c0 2.22 1.8 4.05 4 4.05z"/> <path d="M12.56 6.6A10.97 10.97 0 0 0 14 3.02c.5 2.5 2 4.9 4 6.5s3 3.5 3 5.5a6.98 6.98 0 0 1-11.91 4.97"/>',
    "heart-pulse": '<path d="M2 9.5a5.5 5.5 0 0 1 9.591-3.676.56.56 0 0 0 .818 0A5.49 5.49 0 0 1 22 9.5c0 2.29-1.5 4-3 5.5l-5.492 5.313a2 2 0 0 1-3 .019L5 15c-1.5-1.5-3-3.2-3-5.5"/> <path d="M3.22 13H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"/>',
    "waves": '<path d="M2 12q2.5 2 5 0t5 0 5 0 5 0"/> <path d="M2 19q2.5 2 5 0t5 0 5 0 5 0"/> <path d="M2 5q2.5 2 5 0t5 0 5 0 5 0"/>',
    "zap": '<path d="M15.914 4a1.5 1.5 0 00-2.474-1.561l-9 9A1.5 1.5 0 005.5 14h4.002a.5.5 0 01.471.666L8.086 20a1.5 1.5 0 002.475 1.56l9-9A1.5 1.5 0 0018.5 10h-3.997a.5.5 0 01-.472-.667z"/>',
    "battery-low": '<path d="M22 14v-4"/> <path d="M6 14v-4"/> <rect x="2" y="6" width="16" height="12" rx="2"/>',
    "toilet": '<path d="M7 12h13a1 1 0 0 1 1 1 5 5 0 0 1-5 5h-.598a.5.5 0 0 0-.424.765l1.544 2.47a.5.5 0 0 1-.424.765H5.402a.5.5 0 0 1-.424-.765L7 18"/> <path d="M8 18a5 5 0 0 1-5-5V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8"/>',
    "snail": '<path d="M2 13a6 6 0 1 0 12 0 4 4 0 1 0-8 0 2 2 0 0 0 4 0"/> <circle cx="10" cy="13" r="8"/> <path d="M2 21h12c4.4 0 8-3.6 8-8V7a2 2 0 1 0-4 0v6"/> <path d="M18 3 19.1 5.2"/> <path d="M22 3 20.9 5.2"/>',
    "utensils-crossed": '<path d="m16 2-2.3 2.3a3 3 0 0 0 0 4.2l1.8 1.8a3 3 0 0 0 4.2 0L22 8"/> <path d="M15 15 3.3 3.3a4.2 4.2 0 0 0 0 6l7.3 7.3c.7.7 2 .7 2.8 0L15 15Zm0 0 7 7"/> <path d="m2.1 21.8 6.4-6.3"/> <path d="m19 5-7 7"/>',
    "mouth": '<path d="M2 12a50.5 50.5 0 0020 0 1 1 0 00-1-1"/> <path d="M2.457 11.159a1 1 0 00-.307 1.369 11.59 11.59 0 0019.7 0 1 1 0 00-.308-1.368c-2.426-1.568-3.65-2.284-5.479-3.644a2.6 2.6 0 00-3.373.208 1 1 0 01-1.38 0 2.62 2.62 0 00-3.373-.208c-1.83 1.36-3.053 2.076-5.48 3.643"/>',
    "bed": '<path d="M2 4v16"/> <path d="M2 8h18a2 2 0 0 1 2 2v10"/> <path d="M2 17h20"/> <path d="M6 8v9"/>',
    "hand": '<path d="M18 11V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2"/> <path d="M14 10V4a2 2 0 0 0-2-2a2 2 0 0 0-2 2v2"/> <path d="M10 10.5V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2v8"/> <path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15"/>',
    "bone": '<path d="M17 10c.7-.7 1.69 0 2.5 0a2.5 2.5 0 1 0 0-5 .5.5 0 0 1-.5-.5 2.5 2.5 0 1 0-5 0c0 .81.7 1.8 0 2.5l-7 7c-.7.7-1.69 0-2.5 0a2.5 2.5 0 0 0 0 5c.28 0 .5.22.5.5a2.5 2.5 0 1 0 5 0c0-.81-.7-1.8 0-2.5Z"/>',
    "orbit": '<path d="M20.341 6.484A10 10 0 0 1 10.266 21.85"/> <path d="M3.659 17.516A10 10 0 0 1 13.74 2.152"/> <circle cx="12" cy="12" r="3"/> <circle cx="19" cy="5" r="2"/> <circle cx="5" cy="19" r="2"/>',
    "cloud-drizzle": '<path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/> <path d="M8 19v1"/> <path d="M8 14v1"/> <path d="M16 19v1"/> <path d="M16 14v1"/> <path d="M12 21v1"/> <path d="M12 16v1"/>',
    "scan-face": '<path d="M3 7V5a2 2 0 0 1 2-2h2"/> <path d="M17 3h2a2 2 0 0 1 2 2v2"/> <path d="M21 17v2a2 2 0 0 1-2 2h-2"/> <path d="M7 21H5a2 2 0 0 1-2-2v-2"/> <path d="M8 14s1.5 2 4 2 4-2 4-2"/> <path d="M9 9h.01"/> <path d="M15 9h.01"/>',
    "wind": '<path d="M12.8 19.6A2 2 0 1 0 14 16H2"/> <path d="M17.5 8a2.5 2.5 0 1 1 2 4H2"/> <path d="M9.8 4.4A2 2 0 1 1 11 8H2"/>',
    "bandage": '<path d="M10 10.01h.01"/> <path d="M10 14.01h.01"/> <path d="M14 10.01h.01"/> <path d="M14 14.01h.01"/> <path d="M18 6v12"/> <path d="M6 6v12"/> <rect x="2" y="6" width="20" height="12" rx="2"/>',
    "circle-plus": '<circle cx="12" cy="12" r="10"/> <path d="M8 12h8"/> <path d="M12 8v8"/>',
    "calendar": '<path d="M8 2v3"/> <path d="M16 2v3"/> <rect x="3" y="3" width="18" height="18" rx="2"/> <path d="M3 9h18"/>',
    "calendar-days": '<path d="M8 2v3"/> <path d="M16 2v3"/> <rect x="3" y="3" width="18" height="18" rx="2"/> <path d="M3 9h18"/> <path d="M8 13h.01"/> <path d="M12 13h.01"/> <path d="M16 13h.01"/> <path d="M8 17h.01"/> <path d="M12 17h.01"/> <path d="M16 17h.01"/>',
    "clipboard-list": '<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/> <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/> <path d="M12 11h4"/> <path d="M12 16h4"/> <path d="M8 11h.01"/> <path d="M8 16h.01"/>',
    "settings": '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/> <circle cx="12" cy="12" r="3"/>',
    "info": '<circle cx="12" cy="12" r="10"/> <path d="M12 16v-4"/> <path d="M12 8h.01"/>',
    "external-link": '<path d="M15 3h6v6"/> <path d="M10 14 21 3"/> <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "download": '<path d="M12 15V3"/> <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/> <path d="m7 10 5 5 5-5"/>',
    "search": '<path d="m21 21-4.34-4.34"/> <circle cx="11" cy="11" r="8"/>',
    "trash-2": '<path d="M10 11v6"/> <path d="M14 11v6"/> <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/> <path d="M3 6h18"/> <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "undo-2": '<path d="M9 14 4 9l5-5"/> <path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>',
    "arrow-right": '<path d="M5 12h14"/> <path d="m12 5 7 7-7 7"/>',
    "arrow-left": '<path d="m12 19-7-7 7-7"/> <path d="M19 12H5"/>',
    "pencil": '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/> <path d="m15 5 4 4"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/> <path d="M3 5V19A9 3 0 0 0 21 19V5"/> <path d="M3 12A9 3 0 0 0 21 12"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/> <path d="M16 3.128a4 4 0 0 1 0 7.744"/> <path d="M22 21v-2a4 4 0 0 0-3-3.87"/> <circle cx="9" cy="7" r="4"/>',
    "camera": '<path d="M13.997 4a2 2 0 0 1 1.76 1.05l.486.9A2 2 0 0 0 18.003 7H20a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h1.997a2 2 0 0 0 1.759-1.048l.489-.904A2 2 0 0 1 10.004 4z"/> <circle cx="12" cy="13" r="3"/>',
    "scan-text": '<path d="M3 7V5a2 2 0 0 1 2-2h2"/> <path d="M17 3h2a2 2 0 0 1 2 2v2"/> <path d="M21 17v2a2 2 0 0 1-2 2h-2"/> <path d="M7 21H5a2 2 0 0 1-2-2v-2"/> <path d="M7 8h8"/> <path d="M7 12h10"/> <path d="M7 16h6"/>',
    "clipboard-paste": '<path d="M11 14h10"/> <path d="M16 4h2a2 2 0 0 1 2 2v1.344"/> <path d="m17 18 4-4-4-4"/> <path d="M8 4H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 1.793-1.113"/> <rect x="8" y="2" width="8" height="4" rx="1"/>',
    "image": '<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/> <circle cx="9" cy="9" r="2"/> <path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
    "sparkles": '<path d="M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z"/> <path d="M20 2v4"/> <path d="M22 4h-4"/> <circle cx="4" cy="20" r="2"/>',
    "layout-list": '<rect width="7" height="7" x="3" y="3" rx="1"/> <rect width="7" height="7" x="3" y="14" rx="1"/> <path d="M14 4h7"/> <path d="M14 9h7"/> <path d="M14 15h7"/> <path d="M14 20h7"/>',
    "grid-3x3": '<rect width="18" height="18" x="3" y="3" rx="2"/> <path d="M3 9h18"/> <path d="M3 15h18"/> <path d="M9 3v18"/> <path d="M15 3v18"/>',
    "circle-check": '<circle cx="12" cy="12" r="10"/> <path d="m16 9-5.5 5.5L8 12"/>',
    "rotate-ccw": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/> <path d="M3 3v5h5"/>',
    "user-round": '<circle cx="12" cy="8" r="5"/> <path d="M20 21a8 8 0 0 0-16 0"/>',
    "notebook-pen": '<path d="M13.4 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7.4"/> <path d="M2 6h4"/> <path d="M2 10h4"/> <path d="M2 14h4"/> <path d="M2 18h4"/> <path d="M21.378 5.626a1 1 0 1 0-3.004-3.004l-5.01 5.012a2 2 0 0 0-.506.854l-.837 2.87a.5.5 0 0 0 .62.62l2.87-.837a2 2 0 0 0 .854-.506z"/>',
    "list-checks": '<path d="M13 5h8"/> <path d="M13 12h8"/> <path d="M13 19h8"/> <path d="m3 17 2 2 4-4"/> <path d="m3 7 2 2 4-4"/>',
    "file-spreadsheet": '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/> <path d="M14 2v5a1 1 0 0 0 1 1h5"/> <path d="M8 13h2"/> <path d="M14 13h2"/> <path d="M8 17h2"/> <path d="M14 17h2"/>',
    "ring": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="3" r="1.8" fill="currentColor"/><circle cx="12" cy="12" r="3"/>',
}

# Older names used in templates.
ALIASES = {
    "leaf": "sun", "iv": "hospital", "alert": "triangle-alert", "thermo": "thermometer", "drop": "droplet",
    "heart": "heart-pulse", "wave": "waves", "bolt": "zap", "battery": "battery-low", "pause": "snail",
    "bowl": "utensils-crossed", "report": "clipboard-list", "external": "external-link", "trash": "trash-2",
    "undo": "undo-2", "edit": "pencil", "medicine": "pill-bottle",
}


def paths(name: str) -> Markup:
    key = ALIASES.get(name, name)
    return Markup(PATHS.get(key, PATHS["circle-plus"]))  # noqa: S704 - static, trusted SVG
