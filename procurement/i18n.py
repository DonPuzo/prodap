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
        'back_to_register': 'Back to register',
        'register_heading': 'Procurement Register',
        'nav_home': 'Home',
        'nav_register': 'Register',
        'nav_about': 'About',
        'hero_lead': (
            'publishes every contract, tender, and project it procures — tracked from planning '
            'through completion. No login required, and every status change on this platform is '
            'permanently logged.'
        ),
        'total_projects': 'Total Projects',
        'hero_heading': 'Public Procurement, Made Transparent',
        'audience_public_heading': 'Public & Citizens',
        'audience_public_body': 'Search and browse every procurement record, see current status, and flag anything that looks concerning.',
        'audience_public_cta': 'Browse the register',
        'audience_oversight_heading': 'Oversight, Press & Researchers',
        'audience_oversight_body': 'Download the full dataset for independent analysis — the same data shown on this site, machine-readable.',
        'audience_staff_heading': 'Institution Staff',
        'audience_staff_body': 'Log in to manage annual plans, requisitions, solicitations, and procurement records.',
        'audience_staff_cta': 'Staff login',
        'how_it_works_heading': 'How procurement works here',
        'how_it_works_intro': 'Every project moves through the same statutory stages, in order:',
        'learn_more': 'Learn how this process works',
        'about_heading': 'About ProDAP',
        'about_intro': (
            'ProDAP (Procurement Digital Application Platform) is a public procurement '
            'transparency register. It publishes every stage of a procurement process as it '
            'happens — not a retrospective report — so anyone can see what is being bought, '
            'from whom, for how much, and where the process currently stands.'
        ),
        'about_status_heading': 'What each status means',
        'about_flagging_heading': 'Public scrutiny',
        'about_flagging_body': (
            'Anyone can flag a project as concerning directly from its detail page, no login '
            'required. Flags are visible to both the public and procurement staff — scrutiny '
            'stays visible instead of disappearing into a moderation queue.'
        ),
        'about_accessibility_heading': 'Accessibility & language',
        'about_accessibility_body': (
            'High-contrast mode and adjustable text size are available from the top toolbar on '
            'every page, alongside an English/Pidgin language toggle.'
        ),
        'about_data_heading': 'Open data',
        'about_data_body': 'The complete dataset behind this register is downloadable at any time, in JSON or CSV.',
        'clarifications_heading': 'Clarifications & Questions',
        'ask_question_button': 'Ask a question about this tender',
        'ask_question_placeholder': 'What would you like to know about this tender?',
        'ask_question_submit': 'Submit question',
        'ask_question_submitted_message': 'Thank you — your question has been submitted and will be answered publicly.',
        'clarification_pending_note': 'question(s) awaiting a response.',
        'no_clarifications_yet': 'No questions have been answered for this tender yet.',
        'clarification_closed_note': 'The bidding period for this tender has closed — questions are no longer being accepted.',
        'prequalification_heading': 'Prequalified Bidders',
        'no_prequalified_bidders': 'No prequalification outcomes have been published for this tender yet.',
        'outcome_qualified': 'Qualified',
        'outcome_not_qualified': 'Not Qualified',
        'award_heading': 'Award Decision',
        'bids_heading': 'Bids Received',
        'bid_responsive': 'Responsive',
        'bid_not_responsive': 'Non-responsive',
        'complaints_heading': 'Complaints',
        'file_complaint_button': 'File a complaint about this project',
        'complainant_name_label': 'Your name',
        'complainant_contact_label': 'Email or phone (kept private, used only to follow up)',
        'complaint_description_placeholder': 'Describe your complaint',
        'file_complaint_submit': 'Submit complaint',
        'complaint_submitted_message': 'Thank you — your complaint has been submitted and will be reviewed.',
        'no_complaints_yet': 'No complaints have been resolved for this project.',
        'complaint_pending_note': 'complaint(s) under review.',
        'complaint_upheld': 'Upheld',
        'complaint_dismissed': 'Dismissed',
        'contract_heading': 'Contract',
        'milestones_heading': 'Delivery Milestones',
        'milestone_pending': 'Pending',
        'milestone_completed': 'Completed',
        'completion_heading': 'Final Acceptance',
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
        'back_to_register': 'Go back to register',
        'register_heading': 'Procurement Register',
        'nav_home': 'Home',
        'nav_register': 'Register',
        'nav_about': 'About',
        'hero_lead': (
            'dey publish every contract, tender, and project wey e dey buy — we dey track am '
            'from planning reach finish. No login need, and every status change wey happen for '
            'dis platform dey saved forever.'
        ),
        'total_projects': 'All Projects',
        'hero_heading': 'Public Procurement, Wey You Fit See',
        'audience_public_heading': 'Public & Citizens',
        'audience_public_body': 'Search and see every procurement record, check current status, and flag anything wey dey fishy.',
        'audience_public_cta': 'Go to di register',
        'audience_oversight_heading': 'Oversight, Press & Researchers',
        'audience_oversight_body': 'Download di whole data make you fit analyze am yourself — na di same data wey dey dis site, but machine-readable.',
        'audience_staff_heading': 'Institution Staff',
        'audience_staff_body': 'Login make you fit manage annual plans, requisitions, solicitations, and procurement records.',
        'audience_staff_cta': 'Staff Login',
        'how_it_works_heading': 'How procurement dey work here',
        'how_it_works_intro': 'Every project dey pass through di same stages, one by one:',
        'learn_more': 'Learn how dis process dey work',
        'about_heading': 'About ProDAP',
        'about_intro': (
            'ProDAP (Procurement Digital Application Platform) na public procurement '
            'transparency register. E dey publish every stage of procurement process as e dey '
            'happen — no be after-the-fact report — so anybody fit see wetin dem dey buy, from '
            'who, how much, and where di process reach now.'
        ),
        'about_status_heading': 'Wetin each status mean',
        'about_flagging_heading': 'Public Watch',
        'about_flagging_body': (
            'Anybody fit flag project as fishy straight from di project page, no login need. '
            'Flags dey visible to both public and procurement staff — so scrutiny no go '
            'disappear inside queue.'
        ),
        'about_accessibility_heading': 'Accessibility & Language',
        'about_accessibility_body': (
            'High-contrast mode and text size wey you fit change dey available for di top bar '
            'on every page, plus English/Pidgin language switch.'
        ),
        'about_data_heading': 'Open Data',
        'about_data_body': 'Di full data behind dis register dey available to download anytime, as JSON or CSV.',
        'clarifications_heading': 'Clarifications & Questions',
        'ask_question_button': 'Ask question about dis tender',
        'ask_question_placeholder': 'Wetin you wan know about dis tender?',
        'ask_question_submit': 'Send question',
        'ask_question_submitted_message': 'Thank you — dem don receive your question, dem go answer am make everybody see.',
        'clarification_pending_note': 'question(s) dey wait for answer.',
        'no_clarifications_yet': 'No question don dey answered for dis tender yet.',
        'clarification_closed_note': 'Di bidding period for dis tender don close — dem no dey accept question again.',
        'prequalification_heading': 'Bidders Wey Qualify',
        'no_prequalified_bidders': 'No prequalification result don dey published for dis tender yet.',
        'outcome_qualified': 'E Qualify',
        'outcome_not_qualified': 'E No Qualify',
        'award_heading': 'Wetin Dem Decide (Award)',
        'bids_heading': 'Bids Wey Come',
        'bid_responsive': 'E Correct',
        'bid_not_responsive': 'E No Correct',
        'complaints_heading': 'Complaints',
        'file_complaint_button': 'File complaint about dis project',
        'complainant_name_label': 'Your name',
        'complainant_contact_label': 'Email or phone (we go keep am private, na for follow-up only)',
        'complaint_description_placeholder': 'Explain your complaint',
        'file_complaint_submit': 'Send complaint',
        'complaint_submitted_message': 'Thank you — dem don receive your complaint, dem go review am.',
        'no_complaints_yet': 'No complaint don resolve for dis project.',
        'complaint_pending_note': 'complaint(s) dey under review.',
        'complaint_upheld': 'Dem Uphold Am',
        'complaint_dismissed': 'Dem Reject Am',
        'contract_heading': 'Contract',
        'milestones_heading': 'Delivery Milestones',
        'milestone_pending': 'E Never Ready',
        'milestone_completed': 'E Don Ready',
        'completion_heading': 'Final Acceptance',
    },
}

DEFAULT_LANG = 'en'


def get_strings(lang):
    return STRINGS.get(lang, STRINGS[DEFAULT_LANG])
