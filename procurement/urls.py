from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    # Public pages — no auth, ever.
    path('', views.public_dashboard, name='public_dashboard'),
    path('register/', views.public_register, name='public_register'),
    path('about/', views.public_about, name='public_about'),
    path('projects/<uuid:pk>/', views.public_record_detail, name='public_record_detail'),
    path('projects/<uuid:pk>/flag/', views.flag_record, name='flag_record'),
    path('projects/<uuid:pk>/ask/', views.submit_clarification, name='submit_clarification'),
    path('projects/<uuid:pk>/complain/', views.file_complaint, name='file_complaint'),
    path('export/data.json', views.export_json, name='export_json'),
    path('export/data.csv', views.export_csv, name='export_csv'),
    path('set-lang/<str:lang_code>/', views.set_lang, name='set_lang'),

    # Procurement office backend — login required.
    path('staff/login/', views.StaffLoginView.as_view(), name='staff_login'),
    path('staff/logout/', auth_views.LogoutView.as_view(), name='staff_logout'),
    path('staff/records/', views.staff_record_list, name='staff_record_list'),
    path('staff/records/<uuid:pk>/edit/', views.staff_record_edit, name='staff_record_edit'),
    path('staff/records/<uuid:pk>/transition/', views.staff_status_transition, name='staff_status_transition'),

    # Phase 1-Foundation: annual plans -> requisitions.
    path('staff/plans/', views.staff_plan_list, name='staff_plan_list'),
    path('staff/plans/new/', views.staff_plan_create, name='staff_plan_create'),
    path('staff/plans/<uuid:pk>/', views.staff_plan_detail, name='staff_plan_detail'),
    path('staff/plans/<uuid:pk>/lines/new/', views.staff_plan_line_create, name='staff_plan_line_create'),
    path('staff/plans/<uuid:pk>/submit/', views.staff_plan_submit, name='staff_plan_submit'),
    path('staff/plans/<uuid:pk>/approve/', views.staff_plan_approve, name='staff_plan_approve'),
    path('staff/plans/lines/<uuid:pk>/approve/', views.staff_plan_line_approve, name='staff_plan_line_approve'),

    path('staff/requisitions/', views.staff_requisition_list, name='staff_requisition_list'),
    path('staff/requisitions/new/', views.staff_requisition_create, name='staff_requisition_create'),
    path('staff/requisitions/<uuid:pk>/', views.staff_requisition_detail, name='staff_requisition_detail'),
    path('staff/requisitions/<uuid:pk>/submit/', views.staff_requisition_submit, name='staff_requisition_submit'),
    path(
        'staff/requisitions/<uuid:pk>/confirm-funds/',
        views.staff_requisition_confirm_funds, name='staff_requisition_confirm_funds',
    ),
    path(
        'staff/requisitions/<uuid:pk>/review-packaging/',
        views.staff_requisition_review_packaging, name='staff_requisition_review_packaging',
    ),
    path(
        'staff/requisitions/<uuid:pk>/determine-method/',
        views.staff_requisition_determine_method, name='staff_requisition_determine_method',
    ),
    path(
        'staff/requisitions/<uuid:pk>/create-record/',
        views.staff_requisition_create_record, name='staff_requisition_create_record',
    ),

    # Phase 2 (non-cryptographic slice): solicitation preparation -> advertisement/publication.
    path('staff/records/<uuid:pk>/detail/', views.staff_record_detail, name='staff_record_detail'),
    path(
        'staff/records/<uuid:pk>/solicitations/new/',
        views.staff_solicitation_create, name='staff_solicitation_create',
    ),
    path('staff/solicitations/<uuid:pk>/', views.staff_solicitation_detail, name='staff_solicitation_detail'),
    path(
        'staff/solicitations/<uuid:pk>/approve/',
        views.staff_solicitation_approve, name='staff_solicitation_approve',
    ),
    path(
        'staff/solicitations/<uuid:pk>/publish/',
        views.staff_advertisement_publish, name='staff_advertisement_publish',
    ),
    path(
        'staff/clarifications/<uuid:pk>/answer/',
        views.staff_clarification_answer, name='staff_clarification_answer',
    ),
    path(
        'staff/solicitations/<uuid:pk>/prequalification/add/',
        views.staff_prequalification_add, name='staff_prequalification_add',
    ),
    path(
        'staff/prequalification/<uuid:pk>/review/',
        views.staff_prequalification_review, name='staff_prequalification_review',
    ),
    path('staff/solicitations/<uuid:pk>/bids/add/', views.staff_bid_add, name='staff_bid_add'),
    path('staff/solicitations/<uuid:pk>/award/', views.staff_award_decide, name='staff_award_decide'),
    path('staff/complaints/<uuid:pk>/resolve/', views.staff_complaint_resolve, name='staff_complaint_resolve'),
    path('staff/awards/<uuid:pk>/contract/sign/', views.staff_contract_sign, name='staff_contract_sign'),
    path('staff/contracts/<uuid:pk>/milestones/add/', views.staff_milestone_add, name='staff_milestone_add'),
    path(
        'staff/milestones/<uuid:pk>/complete/',
        views.staff_milestone_complete, name='staff_milestone_complete',
    ),
]
