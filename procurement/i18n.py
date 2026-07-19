"""Minimal English/Nigerian-Pidgin UI string toggle.

Deliberately not Django's full gettext i18n framework: that needs GNU
gettext binaries for compilemessages, which aren't reliably present on
Windows dev machines. A small dict is enough for a two-language toggle of
UI labels (see PRODAP_AGENT_BUILD_PROMPT_V2.md section 7B). Full Yoruba/
Hausa/Igbo translation is Phase 2 item 9 and would warrant real gettext.
"""

STRINGS = {
    'en': {
        'site_tagline': 'Public Procurement Transparency Dashboard',
        'search_placeholder': 'Search by project title or vendor name',
        'active_projects': 'Active Projects',
        'total_contract_value': 'Total Contract Value',
        'status': 'Status',
        'budget_source': 'Budget Source',
        'all': 'All',
        'staff_login_heading': 'Procurement Office Login',
        'username_label': 'Username',
        'password_label': 'Password',
        'login_button': 'Log in',
        'flag_success_message': 'Thank you — this project has been flagged for public scrutiny.',
        'search': 'Search',
        'department': 'Department',
        'cost': 'Cost',
        'no_records': 'No procurement records match your search.',
        'view_details': 'View details',
        'back_to_dashboard': 'Back to dashboard',
        'estimated_cost': 'Estimated Cost',
        'awarded_cost': 'Awarded Cost',
        'procurement_method': 'Procurement Method',
        'location': 'Location',
        'vendor': 'Vendor',
        'planned_timeline': 'Planned Timeline',
        'status_history': 'Status History',
        'no_history': 'No status changes recorded yet.',
        'staff_login': 'Staff Login',
        'download_data': 'Download open data',
        'flag_heading': 'Public Scrutiny',
        'flag_button': 'Flag this project as concerning',
        'flag_note_placeholder': 'Optional: what looks wrong? (e.g. cost seems inflated, project appears stalled)',
        'flag_submit': 'Submit flag',
        'flag_count_zero': 'No one has flagged this project yet.',
        'flag_count_suffix': 'on this project — visible to procurement staff.',
        'already_flagged': "You've already flagged this project from this browser.",
    },
    'pcm': {
        'site_tagline': 'Public Procurement Transparency Dashboard (Wetin Dem Dey Buy For Public)',
        'search_placeholder': 'Search project title or vendor name',
        'active_projects': 'Projects Wey Dey Run',
        'total_contract_value': 'Total Money Wey Dem Spend',
        'status': 'Status',
        'budget_source': 'Money Source',
        'all': 'All',
        'staff_login_heading': 'Procurement Office Login',
        'username_label': 'Username',
        'password_label': 'Password',
        'login_button': 'Log in',
        'flag_success_message': 'Thank you — dis project don dey flag make people watch am well.',
        'search': 'Search',
        'department': 'Department',
        'cost': 'Cost',
        'no_records': 'No record match wetin you dey find.',
        'view_details': 'See more',
        'back_to_dashboard': 'Go back',
        'estimated_cost': 'Cost Wey Dem Plan',
        'awarded_cost': 'Cost Wey Dem Pay',
        'procurement_method': 'How Dem Buy Am',
        'location': 'Location',
        'vendor': 'Company Wey Get Am',
        'planned_timeline': 'Time Wey Dem Plan',
        'status_history': 'Wetin Don Happen So Far',
        'no_history': 'No change dey recorded yet.',
        'staff_login': 'Staff Login',
        'download_data': 'Download the data',
        'flag_heading': 'Make Dem Know',
        'flag_button': 'Flag dis project as fishy',
        'flag_note_placeholder': 'You fit talk wetin dey wrong (e.g. cost too much, work don stop)',
        'flag_submit': 'Send am',
        'flag_count_zero': 'Nobody don flag dis project yet.',
        'flag_count_suffix': 'dey for dis project — procurement people go see am.',
        'already_flagged': 'You don already flag dis project from dis browser.',
    },
}

DEFAULT_LANG = 'en'


def get_strings(lang):
    return STRINGS.get(lang, STRINGS[DEFAULT_LANG])
