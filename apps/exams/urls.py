from django.urls import path

from . import views

app_name = "exams"

urlpatterns = [
    path("", views.exam_list, name="list"),
    path("create/", views.exam_create, name="create"),
    path("<int:pk>/", views.exam_detail, name="detail"),
    path("<int:pk>/edit/", views.exam_edit, name="edit"),
    path("<int:pk>/delete/", views.exam_delete, name="delete"),
    path("<int:pk>/publish/", views.exam_publish, name="publish"),
    path("<int:pk>/access-codes/", views.generate_access_code, name="generate_code"),
    path("redeem-code/", views.redeem_code, name="redeem"),
    path("available/", views.available_exams, name="available"),
    path("<int:pk>/start/", views.start_exam, name="start"),
    path("attempts/<int:attempt_id>/", views.take_exam, name="take"),
    path("attempts/<int:attempt_id>/save/", views.save_answers, name="save"),
    path("attempts/<int:attempt_id>/submit/", views.submit_exam, name="submit"),
    path("attempts/<int:attempt_id>/reconnect/", views.reconnect_exam, name="reconnect"),
    path("attempts/<int:attempt_id>/result/", views.exam_result, name="result"),
    path("my-marks/", views.my_marks, name="my_marks"),
    path("<int:pk>/results/", views.exam_results, name="exam_results"),
    path(
        "attempts/<int:attempt_id>/responses/",
        views.attempt_responses,
        name="attempt_responses",
    ),
    path("<int:pk>/pending-reviews/", views.pending_reviews, name="pending_reviews"),
    path("answers/<int:answer_id>/grade/", views.grade_answer, name="grade_answer"),
    path("question-banks/", views.question_bank_list, name="question_banks"),
    path("question-banks/import/sample.csv", views.question_import_sample, name="question_import_sample"),
    path("question-banks/create/", views.question_bank_create, name="question_bank_create"),
    path("question-banks/<int:bank_id>/", views.question_bank_detail, name="question_bank_detail"),
    path("question-banks/<int:bank_id>/builder/", views.question_bank_builder, name="question_bank_builder"),
    path("question-banks/<int:bank_id>/library/", views.question_bank_library, name="question_bank_library"),
    path("question-banks/<int:bank_id>/import/", views.question_bank_import, name="question_bank_import"),
    path("question-banks/<int:bank_id>/questions/add/", views.question_add, name="question_add"),
    path(
        "question-banks/<int:bank_id>/questions/<int:question_id>/edit/",
        views.question_edit,
        name="question_edit",
    ),
    path(
        "question-banks/<int:bank_id>/questions/<int:question_id>/delete/",
        views.question_delete,
        name="question_delete",
    ),
    path("<int:pk>/questions/", views.exam_questions, name="exam_questions"),
    path("<int:pk>/preview/", views.exam_preview, name="exam_preview"),
    path("<int:pk>/submit-approval/", views.exam_submit_for_approval, name="submit_approval"),
    path("admin/pending/", views.admin_pending_exams, name="admin_pending_exams"),
    path("admin/<int:pk>/review/", views.admin_exam_review, name="admin_exam_review"),
]
