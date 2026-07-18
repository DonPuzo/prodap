from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    # Public dashboard — no auth, ever.
    path('', views.public_dashboard, name='public_dashboard'),
    path('projects/<uuid:pk>/', views.public_record_detail, name='public_record_detail'),
    path('export/data.json', views.export_json, name='export_json'),
    path('export/data.csv', views.export_csv, name='export_csv'),
    path('set-lang/<str:lang_code>/', views.set_lang, name='set_lang'),

    # Procurement office backend — login required.
    path('staff/login/', auth_views.LoginView.as_view(template_name='staff/login.html'), name='staff_login'),
    path('staff/logout/', auth_views.LogoutView.as_view(), name='staff_logout'),
    path('staff/records/', views.staff_record_list, name='staff_record_list'),
    path('staff/records/new/', views.staff_record_create, name='staff_record_create'),
    path('staff/records/<uuid:pk>/edit/', views.staff_record_edit, name='staff_record_edit'),
    path('staff/records/<uuid:pk>/transition/', views.staff_status_transition, name='staff_status_transition'),
]
