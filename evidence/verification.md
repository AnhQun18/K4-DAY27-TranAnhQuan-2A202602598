# Kết quả kiểm tra lab — 15/10/2026

## Phạm vi

Hoàn thiện 5 bài với SQLite, theo lựa chọn của người dùng. Không triển khai PostgreSQL hoặc các bonus. Người dùng đã tự bấm Confirm & Post để đăng bản review đã sửa; không tạo Git commit hay merge PR.

## Kiểm thử tự động

Lệnh: `uv run python -X utf8 -m unittest discover -s tests -v`.

Kết quả: **16/16 tests đạt**. GitHub và LLM được mock; dùng database riêng, không ghi vào dữ liệu lab.

- Routing ở các mốc 0.579, 0.58, 0.729 và 0.73.
- Approve, Reject, Edit và auto-post; Edit đăng đúng nội dung đã sửa.
- Khôi phục checkpoint sau khi đóng và mở lại kết nối SQLite.
- Escalation CLI; escalation UI phải xác nhận cuối, hỗ trợ Reject/Edit.
- Giữ Q&A, tiếp tục node synthesize sau lỗi API; không fetch lại PR.
- Yêu cầu quyết định rõ ràng; không cho nội dung Edit rỗng.
- Lỗi đăng GitHub được ghi riêng, không coi là quyết định Reject của người review.
- Audit không lặp sự kiện chờ khi resume; reviewer được ghi sau escalation.
- Replay tính worst_risk theo high > med > low.
- Streamlit AppTest: khôi phục phiên chờ, Edit rỗng hiện lỗi, Cancel và Reject hoạt động.

## Kiểm tra trên trình duyệt bên cạnh

Phiên: `e654e98e-70bc-464c-ab82-a07f036a3ce1`.

PR: https://github.com/VinUni-AI20k/PR-Demo/pull/2

1. Khôi phục phiên escalation, confidence ban đầu 55%.
2. Trả lời 4 câu hỏi theo đánh giá reviewer; các biện pháp đề xuất được ghi rõ là yêu cầu chưa triển khai, không giả định đã có trong code.
3. Gặp OpenRouter 402: request 1000 token vượt ngân sách được báo là 880 token.
4. Giảm output mặc định xuống 600 token và dùng prompt gọn hơn; thêm biến cấu hình LLM_MAX_TOKENS.
5. Khởi động lại đúng server Streamlit của lab, giữ nguyên database/checkpoint.
6. Dùng Retry Saved Step, synthesize thành công; confidence mới 40%, giữ các rủi ro chưa giải quyết.
7. Refresh vẫn ở bước xác nhận cuối, cùng thread; audit có 7 sự kiện và chưa có sự kiện commit.
8. Kiểm tra chế độ Edit/preview trên giao diện. Đối chiếu diff checkpoint: sửa nhận định thiếu timeout (code đã có 10 giây), bổ sung SQL nối chuỗi và endpoint HTTP mang bearer token mà LLM bỏ sót. Nội dung đã chỉnh được lưu trong `review-draft.md`.
9. Người dùng tự bấm Confirm & Post. UI xác nhận `committed_after_escalation`; audit có thêm `confirm_synthesize` và `commit` với `decision='edit'`, reviewer `AnhQun18`.
10. Phiên hoàn tất có 9 sự kiện, đúng 1 sự kiện commit; không đăng lại khi kiểm tra kết quả.

Ảnh minh chứng: `escalation-synthesized.jpg`, `review-ready.jpg`, `review-confirmation.jpg` và `review-posted.jpg`. Timeline của phiên thật sau khi đăng: `audit-replay.txt`.

## Lưu ý

- Đây là kiểm tra phục hồi server thực tế và mở lại SQLite trong tests; chưa mô phỏng mọi thời điểm process bị kill giữa một request đăng GitHub và ghi checkpoint.
- Không cam kết exactly-once khi process chết ngay sau GitHub chấp nhận comment nhưng trước khi checkpoint được lưu.
- LLM confidence có thể thay đổi; test routing dùng dữ liệu cố định.
- Thư viện hiện cảnh báo PRAnalysis chưa đăng ký rõ kiểu msgpack; phiên bản đang cài vẫn khôi phục được. Nếu nâng LangGraph cần kiểm tra lại cấu hình serializer.
- `.env`, token, database và log runtime không nằm trong bài nộp.
