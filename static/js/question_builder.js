(function () {
    const form = document.getElementById("qb-save-form");
    if (!form) return;

    const questionText = document.getElementById("qb-question-text");
    const explanation = document.getElementById("qb-explanation");
    const optionsEditor = document.getElementById("qb-options-editor");
    const addOptionBtn = document.getElementById("qb-add-option");
    const questionTypeSelect = document.getElementById("id_question_type");

    let activeField = questionText || explanation;

    function trackField(field) {
        if (!field) return;
        field.addEventListener("focus", () => {
            activeField = field;
        });
    }

    trackField(questionText);
    trackField(explanation);

    function wrapSelection(textarea, before, after, placeholder) {
        if (!textarea) return;
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        const text = textarea.value;
        const selected = text.substring(start, end) || placeholder || "text";
        textarea.value = text.substring(0, start) + before + selected + after + text.substring(end);
        const cursorStart = start + before.length;
        const cursorEnd = cursorStart + selected.length;
        textarea.focus();
        textarea.setSelectionRange(cursorStart, cursorEnd);
    }

    document.querySelectorAll("[data-qb-format]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const format = btn.dataset.qbFormat;
            const field = activeField || questionText;
            if (!field) return;

            const formats = {
                bold: ["**", "**", "bold text"],
                italic: ["*", "*", "italic text"],
                underline: ["__", "__", "underlined text"],
                formula: ["$", "$", "x^2"],
                code: ["`", "`", "code"],
            };
            const spec = formats[format];
            if (spec) wrapSelection(field, spec[0], spec[1], spec[2]);
        });
    });

    function reindexOptions() {
        if (!optionsEditor) return;
        const rows = optionsEditor.querySelectorAll(".qb-option-row");
        rows.forEach((row, index) => {
            const key = String.fromCharCode(65 + index);
            row.dataset.optionIndex = String(index);
            const keyEl = row.querySelector(".qb-option-key");
            if (keyEl) keyEl.textContent = key;
            const input = row.querySelector(".qb-option-input");
            if (input) input.name = `option_label_${index}`;
            const radio = row.querySelector('input[type="radio"]');
            if (radio) {
                radio.name = "correct_option_index";
                radio.value = String(index);
            }
        });
    }

    function bindRemoveButtons() {
        if (!optionsEditor) return;
        optionsEditor.querySelectorAll(".qb-option-remove").forEach((btn) => {
            btn.onclick = () => {
                const rows = optionsEditor.querySelectorAll(".qb-option-row");
                if (rows.length <= 2) {
                    window.alert("Each question needs at least two answer options.");
                    return;
                }
                btn.closest(".qb-option-row").remove();
                reindexOptions();
                updateCorrectHighlight();
            };
        });
    }

    function updateCorrectHighlight() {
        if (!optionsEditor) return;
        optionsEditor.querySelectorAll(".qb-option-row").forEach((row) => {
            row.classList.remove("correct");
        });
        const checked = optionsEditor.querySelector('input[type="radio"]:checked');
        if (checked) {
            checked.closest(".qb-option-row")?.classList.add("correct");
        }
    }

    if (optionsEditor) {
        optionsEditor.addEventListener("change", (event) => {
            if (event.target.matches('input[type="radio"]')) {
                updateCorrectHighlight();
            }
        });
        bindRemoveButtons();
        updateCorrectHighlight();
    }

    document.querySelectorAll(".qb-options-editor--tf").forEach((editor) => {
        editor.addEventListener("change", (event) => {
            if (!event.target.matches('input[type="radio"]')) return;
            editor.querySelectorAll(".qb-option-row").forEach((row) => {
                row.classList.remove("correct");
            });
            event.target.closest(".qb-option-row")?.classList.add("correct");
        });
    });

    if (addOptionBtn && optionsEditor) {
        addOptionBtn.addEventListener("click", () => {
            const index = optionsEditor.querySelectorAll(".qb-option-row").length;
            if (index >= 6) {
                window.alert("You can add up to six options (A–F).");
                return;
            }
            const key = String.fromCharCode(65 + index);
            const row = document.createElement("div");
            row.className = "qb-option-row";
            row.dataset.optionIndex = String(index);
            row.innerHTML = `
                <span class="qb-option-key">${key}</span>
                <input type="text" class="qb-option-input qb-input" name="option_label_${index}" value="" placeholder="Option ${key}">
                <label class="qb-option-correct-label">
                    <input type="radio" name="correct_option_index" value="${index}">
                    <span>Correct</span>
                </label>
                <button type="button" class="qb-option-remove" aria-label="Remove option">&times;</button>
            `;
            optionsEditor.appendChild(row);
            reindexOptions();
            bindRemoveButtons();
            row.querySelector(".qb-option-input")?.focus();
        });
    }

    // ── Answer editor follows the Type dropdown ──────────────────────────────
    // All three editors are in the DOM. Only the one matching the selected type
    // is shown, and the others have their inputs disabled so the browser never
    // posts MCQ options for a short-answer question (QuestionForm.clean picks
    // fields by type, and a stale option_label_0 would resurrect old choices).
    const answerPanels = form.querySelectorAll("[data-qb-answer-panel]");

    function showAnswerPanelFor(type) {
        answerPanels.forEach((panel) => {
            const matches = panel.dataset.qbAnswerPanel === type;
            panel.hidden = !matches;
            panel.querySelectorAll("input, textarea, select, button").forEach((field) => {
                field.disabled = !matches;
            });
        });
    }

    if (answerPanels.length) {
        showAnswerPanelFor(questionTypeSelect ? questionTypeSelect.value : "mcq");
    }

    if (questionTypeSelect) {
        questionTypeSelect.addEventListener("change", () => {
            showAnswerPanelFor(questionTypeSelect.value);
        });
    }

    form.addEventListener("submit", () => {
        reindexOptions();
    });
})();
