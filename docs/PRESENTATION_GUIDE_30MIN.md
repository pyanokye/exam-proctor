# 30-Minute Presentation Guide

**Project:** Online Exam Proctoring System Using Deep Learning  
**Institution:** KNUST — Department of Computer Science  
**Suggested duration:** 12–15 min talk + 5 min demo + Q&A

---

## Slide Deck Outline (12 slides)

### Slide 1 — Title
- **Online Exam Proctoring System Using Deep Learning**
- Your name, index number, supervisor, KNUST, date

**Say:** "Good [morning/afternoon]. I present my work on an AI-powered online examination platform designed to detect cheating—especially use of foreign materials like phones and books—during remote exams at KNUST."

---

### Slide 2 — Problem (30 sec)
**Headline:** Online exams can't see what students are doing

- KNUST uses online/blended assessment more than ever
- Browser lockdown stops tab switching — **but not phones, books, or notes**
- Invigilators in hall can see desks; remote platforms usually **cannot**

**Say:** "The core problem is a visibility gap. Rule-based systems know what happens in the browser, not in the physical room."

---

### Slide 3 — Research Gap (Literature) (45 sec)
**Headline:** What existing systems miss

| Detects | Doesn't detect |
|---------|----------------|
| Login, timer, tab switch | Phone below desk |
| Copy/paste block | Printed notes beside screen |
| | Second person off-camera |

**Gap:** *Inability to automatically detect foreign materials during remote examinations*

**Say:** "Chapter 2 shows this gap across LMS tools, honor codes, and browser lockdown. Live human proctors help but don't scale. We need automated visual monitoring."

---

### Slide 4 — Research Aim & Hypothesis (30 sec)
- **Aim:** Design, build, and evaluate a YOLOv5-based proctoring system
- **H₁:** YOLOv5 integration **significantly improves** automated violation detection vs manual/rule-based proctoring

**Say:** "My hypothesis is that deep learning object detection closes the foreign-materials gap measurably."

---

### Slide 5 — System Overview (45 sec)
**Three roles:** Admin → Teacher → Student

**Flow:**
1. Student registers with profile photo
2. Redeems exam code → identity verified (face + ID)
3. Lockdown + webcam proctoring during exam
4. Violations logged → teacher reviews flagged sessions

**Say:** "It's a full examination platform, not just a detection script. Proctoring is embedded in the exam lifecycle."

---

### Slide 6 — Architecture (45 sec)
Use diagram from your thesis / SYSTEM_DESIGN:

```
Browser (webcam + lockdown JS)
    → Django API
    → Celery worker
    → YOLOv5 + pose model
    → Violation log + WebSocket alerts
```

**Say:** "Frames upload every second. Inference runs in a background worker so the exam UI stays responsive. Results push back instantly via WebSocket."

---

### Slide 7 — What YOLO Detects (45 sec)
**Foreign materials & behaviours:**
- Mobile phone
- Books / notes / secondary devices
- Student absent from frame
- Multiple people
- Sustained look-away (5 seconds)

**Enforcement (Level 1, the default):** 5-strike tolerance → acknowledged warnings → auto-terminate on the 5th strike
Per-exam strictness: *Not strict* (no checks) · *Level 1* (5 strikes) · *Level 2* (zero tolerance — any violation ends the exam)

**Say:** "This directly addresses the literature gap. YOLO sees objects humans would see in an exam hall."

---

### Slide 8 — Methodology Summary (30 sec)
- **Design:** Applied experimental + Design Science Research
- **Development:** Agile sprints
- **Stack:** Python, Django, YOLOv5, OpenCV, JavaScript
- **Evaluation:** Precision, recall, F1, latency, usability testing (5+ runs per scenario)

**Say:** "Chapter 3 details this. I built a working prototype and tested it under controlled conditions."

---

### Slide 9 — Demo Plan (1–2 min live)
**Show in this order:**
1. Login as student → open exam → **identity verification** (face verified before Begin)
2. Begin exam → proctor feed with **FACE VERIFIED**
3. Hold phone near camera → **strike alert** appears
4. (Optional) Teacher portal → flagged session with violation snapshot

**Fallback:** Screenshots if live demo fails.

**Say:** "Verification happens before the student can start. Detection runs continuously during the exam."

---

### Slide 10 — Key Results (fill from your Ch. 4 or testing notes)
Template if results chapter not final:

| Metric | Target / observation |
|--------|----------------------|
| Phone detection | Detected when held in frame |
| Frame interval | 1 second |
| Inference latency | ~40–50 ms (CPU, local test) |
| False positives | Reduced via sustained look-away rule |

**Say:** "On commodity hardware, inference is fast enough for near-real-time proctoring without a GPU."

---

### Slide 11 — Significance & Contribution
- Addresses **foreign-material detection gap** for KNUST-style remote exams
- Scalable alternative to live human proctors
- Open-source stack (Django, YOLOv5)
- Reusable for future fairness audits and model training

---

### Slide 12 — Conclusion & Future Work
**Conclusion:** Integrated YOLOv5 proctoring + lockdown + ID verification improves integrity of online exams.

**Future work:**
- GPU deployment for concurrent sessions
- Custom-trained model on local violation dataset
- Fairness audit across demographic groups
- Pilot with real course cohort at KNUST

**Say:** "Thank you. I'm happy to take questions."

---

## Anticipated Q&A (quick answers)

**Q: Why YOLOv5 and not YOLOv8?**  
A: YOLOv5 balances speed and accuracy on CPU; we also use YOLOv8n-pose for head/body cues. YOLOv5 handles object classes like phone and book directly.

**Q: What about privacy?**  
A: We store violation snapshots and logs, not full video. Students consent before testing; data minimisation aligns with our ethics section.

**Q: Can students cheat by hiding the phone off-camera?**  
A: Partial occlusion is a limitation. Shorter frame intervals and sustained-detection rules reduce the blind window; no system is perfect.

**Q: False positives?**  
A: Look-away uses a 5-second sustained timer. Object violations require model confidence thresholds. Teachers review flagged evidence.

**Q: Firebase or SQLite?**  
A: [Use what matches your submitted Ch. 3 — SQLite for prototype persistence in development.]

**Q: How is this different from ProctorU / Respondus?**  
A: Locally developed, tailored to KNUST workflow (question banks, admin approval, exam codes), and evaluated as a research artefact rather than a commercial black box.

---

## Pre-Presentation Checklist (next 30 min)

- [ ] Start server: `python manage.py runserver` (or daphne for WebSocket)
- [ ] Celery worker running if not in eager mode
- [ ] Test student account with profile photo loaded
- [ ] Test exam with proctoring enabled
- [ ] Phone ready for detection demo
- [ ] Teacher account open in second tab for flagged session view
- [ ] Hard refresh exam page (Cmd+Shift+R)
- [ ] Close unrelated tabs; allow camera + fullscreen permissions

---

## One-Paragraph Elevator Pitch (memorise this)

> Remote examinations at universities like KNUST rely heavily on browser-based platforms that cannot see whether students introduce phones, books, or notes during the test. My project addresses this research gap by building a Django web examination system integrated with YOLOv5 deep learning for real-time webcam proctoring. The system verifies identity before the exam starts, enforces lockdown rules, detects prohibited objects and behaviours, applies a five-strike policy, and gives teachers evidence to review flagged sessions. I evaluated the prototype using standard object-detection metrics and controlled test scenarios, demonstrating that automated visual proctoring is feasible on ordinary student hardware and can strengthen academic integrity in online assessments.
