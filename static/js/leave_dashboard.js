// Edit Modal File Upload Config
const EDIT_MAX_FILES       = 3;
const EDIT_PDF_MAX_BYTES   = 1 * 1024 * 1024;    // 1MB hard limit for PDF
const EDIT_IMG_WARN_BYTES  = 5 * 1024 * 1024;    // 5MB warn for images
const EDIT_IMG_BLOCK_BYTES = 10 * 1024 * 1024;   // 10MB hard block for images
const EDIT_MAX_TOTAL_BYTES = 3 * 1024 * 1024;    // 3MB total
let editFileCount = 0;  // tracks new file rows added (separate from existing docs)

function formatEditBytes(bytes) {
    return (bytes / 1024 / 1024).toFixed(2) + ' MB';
}

function isPdf(filename) {
    return filename.toLowerCase().endsWith('.pdf');
}

function updateEditTotalSize() {
    const inputs = document.querySelectorAll('#editFileUploadArea .edit-file-input');
    let total = 0, hasError = false;

    inputs.forEach(input => {
        const hint = document.getElementById(`editSizeHint${input.dataset.index}`);
        if (input.files && input.files[0]) {
            const file = input.files[0];
            const size = file.size;
            total += size;

            if (isPdf(file.name)) {
                // PDFs cannot be compressed — hard block at 1MB
                if (size > EDIT_PDF_MAX_BYTES) {
                    hint.textContent = `⚠ ${formatEditBytes(size)} — PDF exceeds 1MB limit`;
                    hint.className = 'text-danger file-size-hint';
                    hasError = true;
                } else {
                    hint.textContent = `PDF · ${formatEditBytes(size)}`;
                    hint.className = 'text-muted file-size-hint';
                }
            } else {
                // Images will be compressed server-side — only hard block at 10MB per file
                if (size > EDIT_IMG_BLOCK_BYTES) {
                    hint.textContent = `⚠ ${formatEditBytes(size)} — image too large (max 10MB before compression)`;
                    hint.className = 'text-danger file-size-hint';
                    hasError = true;
                } else if (size > EDIT_IMG_WARN_BYTES) {
                    hint.textContent = `${formatEditBytes(size)} — will be compressed ✓`;
                    hint.className = 'text-warning file-size-hint';
                } else {
                    hint.textContent = `${formatEditBytes(size)} — will be compressed ✓`;
                    hint.className = 'text-muted file-size-hint';
                }
            }
        } else if (hint) {
            hint.textContent = '';
        }
    });

    document.getElementById('editTotalSizeDisplay').textContent = formatEditBytes(total);
    const totalError = document.getElementById('editTotalSizeError');

    // Images will be compressed server-side
    const totalHardLimit = EDIT_MAX_FILES * EDIT_IMG_BLOCK_BYTES;
    if (total > totalHardLimit) {
        totalError.textContent = `Total file size too large.`;
        totalError.style.display = 'block';
        hasError = true;
    } else {
        totalError.style.display = 'none';
    }

    return !hasError;
}

function addEditFileRow(index) {
    const area = document.getElementById('editFileUploadArea');
    const row  = document.createElement('div');
    row.className = 'mb-2 edit-file-row';
    row.id = `editFileRow${index}`;
    row.innerHTML = `
        <div class="input-group">
            <input type="file"
                   class="form-control edit-file-input"
                   name="upload_documents"
                   accept=".pdf,.jpg,.jpeg,.png"
                   data-index="${index}">
            <button type="button"
                    class="btn btn-outline-danger edit-remove-file-btn"
                    data-index="${index}">
                <i class="fas fa-times"></i>
            </button>
        </div>
        <small class="text-muted file-size-hint" id="editSizeHint${index}"></small>
    `;

    // bind change
    row.querySelector('.edit-file-input').addEventListener('change', function() {
        const btn = row.querySelector('.edit-remove-file-btn');
        btn.style.display = this.files && this.files[0] ? 'inline-block' : 'none';
        updateEditTotalSize();
    });

    // bind remove
    row.querySelector('.edit-remove-file-btn').addEventListener('click', function() {
        row.remove();
        editFileCount--;
        updateEditAddBtnState();
        updateEditTotalSize();
    });

    area.appendChild(row);
}

function updateEditAddBtnState() {
    const existingCount = document.querySelectorAll('#editExistingDocs .col-md-4').length;
    const newCount = document.querySelectorAll('#editFileUploadArea .edit-file-row').length;
    const addBtn = document.getElementById('editAddFileBtn');
    if (!addBtn) return; 
    addBtn.disabled = (existingCount + newCount) >= EDIT_MAX_FILES;
}

function formatDateDisplay(dateStr) {
    const d = new Date(dateStr);
    const day = String(d.getDate()).padStart(2, "0");
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const year = d.getFullYear();
    return `${day}/${month}/${year}`;
}

// Open View Leave Modal
window.openViewLeaveModal = function(leave) {
    // Basic Info
    document.getElementById('viewRequesterName').textContent = leave.requester_name || 'N/A';
    document.getElementById('viewDepartment').textContent = leave.department || 'N/A';
    document.getElementById('viewLeaveType').textContent = leave.leave_type || 'N/A';
    document.getElementById('viewLeaveReason').textContent = leave.leave_reason || 'N/A';
    document.getElementById('viewStartDate').textContent = leave.start_date || 'N/A';
    document.getElementById('viewEndDate').textContent = leave.end_date || 'N/A';
    document.getElementById('viewNoDays').textContent = leave.no_days || 'N/A';
    
    // Status with badge
    const statusBadges = {
        'Pending': '<span class="status-badge status-pending">Pending</span>',
        'Acknowledged': '<span class="status-badge status-acknowledged">Acknowledged</span>',
        'Approved': '<span class="status-badge status-approved">Approved</span>',
        'Rejected': '<span class="status-badge status-rejected">Rejected</span>'
    };
    document.getElementById('viewStatus').innerHTML = statusBadges[leave.display_status] || leave.display_status;
    
    // Remarks - show either single day or multi-day
    if (leave.start_date === leave.end_date) {
        document.getElementById('viewRemarkSection').style.display = 'block';
        document.getElementById('viewMultiDayRemarkSection').style.display = 'none';
        document.getElementById('viewRemark').textContent = leave.remark || 'Whole Day';
    } else {
        document.getElementById('viewRemarkSection').style.display = 'none';
        document.getElementById('viewMultiDayRemarkSection').style.display = 'block';
        document.getElementById('viewStartRemark').textContent = leave.start_remark || 'Whole Day';
        document.getElementById('viewEndRemark').textContent = leave.end_remark || 'Whole Day';
    }
    
    // Document    
    const docSection = document.getElementById('viewDocumentSection');
    docSection.style.display = 'none'; // hide while loading

    fetch(`/leave_documents/${leave.leave_id}`)
        .then(r => r.json())
        .then(docs => {
            const docContainer = document.getElementById('viewDocumentContainer');
            docContainer.innerHTML = '';

            if (!docs.length) {
                docSection.style.display = 'none';
                return;
            }

            docSection.style.display = 'block';
            docs.forEach(doc => {
                const isPdf = doc.filename.toLowerCase().endsWith('.pdf');
                const col = document.createElement('div');
                col.className = 'col-md-4 mb-2';
                col.innerHTML = `
                    <div class="border rounded p-2 text-center">
                        ${isPdf
                            ? `<i class="fas fa-file-pdf fa-2x text-danger mb-1"></i>`
                            : `<img src="${doc.url}"
                                    class="img-fluid mb-1 rounded"
                                    style="max-height:80px;object-fit:cover;">`
                        }
                        <div class="small text-truncate" title="${doc.original_name}">
                            ${doc.original_name}
                        </div>
                        <div class="small text-muted">
                            ${doc.file_size ? (doc.file_size/1024).toFixed(0) + 'KB' : ''}
                        </div>
                        <a href="${doc.url}" target="_blank"
                           class="btn btn-sm btn-outline-primary mt-1">
                            <i class="fas fa-eye"></i> View
                        </a>
                    </div>`;
                docContainer.appendChild(col);
            });
        })
        .catch(() => {
            docSection.style.display = 'none';
        });
    
    // Reject Reason (if rejected)
    const rejectSection = document.getElementById('viewRejectReasonSection');
    if (leave.display_status === 'Rejected' && leave.reject_reason) {
        rejectSection.style.display = 'block';
        document.getElementById('viewRejectReason').textContent = leave.reject_reason;
    } else {
        rejectSection.style.display = 'none';
    }
    
    // Dates
    document.getElementById('viewCreatedAt').textContent = leave.created_at || 'N/A';
    document.getElementById('viewStatusUpdate').textContent = leave.status_update || 'N/A';
    
    // Show modal
    new bootstrap.Modal(document.getElementById('viewLeaveModal')).show();
};

window.openEditLeaveModal = function(leave) {
    if (leave.status !== "Pending") {
        alert("Only pending leave requests can be edited.");
        return;
    }

    const form = document.getElementById("editLeaveForm");
    form.action = `/update_leave/${leave.leave_id}`;

    document.getElementById("editLeaveType").value   = leave.leave_type;
    document.getElementById("editLeaveReason").value = leave.leave_reason;

    const startInput = document.getElementById("editStartDate");
    const endInput   = document.getElementById("editEndDate");
    startInput.value = leave.start_date;
    endInput.value   = leave.end_date;

    document.getElementById("prevStartDate").textContent = `Previous: ${formatDateDisplay(leave.start_date)}`;
    document.getElementById("prevEndDate").textContent   = `Previous: ${formatDateDisplay(leave.end_date)}`;
    document.getElementById("editRemark").value       = leave.remark       || "Whole Day";
    document.getElementById("editStartRemark").value  = leave.start_remark || "Whole Day";
    document.getElementById("editEndRemark").value    = leave.end_remark   || "Whole Day";

    // ── Reset upload area immediately ──
    const uploadArea = document.getElementById('editFileUploadArea');
    uploadArea.innerHTML = '';
    editFileCount = 0;
    document.getElementById('editTotalSizeDisplay').textContent = '0 MB';
    document.getElementById('editTotalSizeError').style.display = 'none';

    // Add first empty file row
    addEditFileRow(editFileCount++);
    // NOTE: updateEditAddBtnState() called AFTER fetch resolves, not here

    // Re-bind add button each time modal opens
    const addBtn = document.getElementById('editAddFileBtn');
    const newAddBtn = addBtn.cloneNode(true);
    addBtn.parentNode.replaceChild(newAddBtn, addBtn);
    newAddBtn.addEventListener('click', function() {
        const existingCount = document.querySelectorAll('#editExistingDocs .col-md-4').length;
        const newCount      = document.querySelectorAll('#editFileUploadArea .edit-file-row').length;
        if (existingCount + newCount >= EDIT_MAX_FILES) {
            this.disabled = true;
            return;
        }
        addEditFileRow(editFileCount++);
        updateEditAddBtnState();
    });

    // ── Load existing documents ──
    const docsDiv    = document.getElementById('editExistingDocs');
    const docsParent = document.getElementById('editViewDocumentDiv');
    docsDiv.innerHTML = '<small class="text-muted">Loading...</small>';
    docsParent.style.display = 'block';

    fetch(`/leave_documents/${leave.leave_id}`)
        .then(r => r.json())
        .then(docs => {
            docsDiv.innerHTML = '';

            if (!docs.length) {
                // CHANGED: no legacy upload_document check needed anymore
                docsParent.style.display = 'none';
            } else {
                docsParent.style.display = 'block';
                docs.forEach(doc => {
                    const isPdf = doc.filename.toLowerCase().endsWith('.pdf');
                    const col   = document.createElement('div');
                    col.className = 'col-md-4';
                    col.innerHTML = `
                        <div class="border rounded p-2 text-center" id="docCard_${doc.id}">
                            ${isPdf
                                ? `<i class="fas fa-file-pdf fa-2x text-danger mb-1"></i>`
                                : `<img src="${doc.url}" class="img-fluid mb-1 rounded" style="max-height:80px;object-fit:cover;">`
                            }
                            <div class="small text-truncate" title="${doc.original_name}">${doc.original_name}</div>
                            <div class="small text-muted">${doc.file_size ? (doc.file_size/1024).toFixed(0)+'KB' : ''}</div>
                            <div class="mt-1">
                                <a href="${doc.url}" target="_blank" class="btn btn-sm btn-outline-primary me-1">
                                    <i class="fas fa-eye"></i>
                                </a>
                                <button type="button" class="btn btn-sm btn-outline-danger delete-doc-btn"
                                        data-doc-id="${doc.id}">
                                    <i class="fas fa-trash"></i>
                                </button>
                            </div>
                        </div>`;
                    docsDiv.appendChild(col);
                });

                docsDiv.querySelectorAll('.delete-doc-btn').forEach(btn => {
                    btn.addEventListener('click', function() {
                        if (!confirm('Delete this document?')) return;
                        fetch(`/leave_document/delete/${this.dataset.docId}`, { method: 'POST' })
                            .then(r => r.json())
                            .then(data => {
                                if (data.success) {
                                    document.getElementById(`docCard_${this.dataset.docId}`)
                                        ?.closest('.col-md-4')?.remove();
                                    if (!docsDiv.children.length) docsParent.style.display = 'none';
                                    updateEditAddBtnState();  // re-evaluate after delete
                                } else {
                                    alert(data.message);
                                }
                            });
                    });
                });
            }

            // CHANGED: always call after docs rendered so count is accurate
            updateEditAddBtnState();
        })
        .catch(() => {
            docsDiv.innerHTML = '<small class="text-danger">Failed to load documents.</small>';
            updateEditAddBtnState();  // still update even on error
        });

    initLeaveForm({
        formId: "editLeaveForm",
        leaveReasonId: null,
        otherReasonFieldId: null,
        otherReasonInputId: null,
        startDateId: "editStartDate",
        endDateId: "editEndDate",
        noDaysId: "editNoDays",
        remarkId: "editRemark",
        startRemarkId: "editStartRemark",
        endRemarkId: "editEndRemark",
        endRemarkFieldId: "editEndRemarkField",
        remarkLabelId: "editRemarkLabel",
        startRemarkLabelId: "editStartRemarkLabel",
        startDateErrorId: "editStartDateError",
        endDateErrorId: "editEndDateError",
        noDaysErrorId: "editNoDaysError",
        excludeLeaveId: leave.leave_id
    });

    startInput.dispatchEvent(new Event("change"));
    endInput.dispatchEvent(new Event("change"));

    new bootstrap.Modal(document.getElementById("editLeaveModal")).show();
};

// Form validation
document.getElementById("editLeaveForm").addEventListener("submit", function(e) {
    const required = ["editLeaveType", "editLeaveReason", "editStartDate", "editEndDate", "editNoDays"];
    for (const id of required) {
        const el = document.getElementById(id);
        if (!el || !el.value.trim()) {
            alert("Please fill all required fields before submitting.");
            e.preventDefault();
            return false;
        }
    }

    const newInputs = document.querySelectorAll('#editFileUploadArea .edit-file-input');
    const hasNewFiles = Array.from(newInputs).some(input => input.files && input.files[0]);

    if (hasNewFiles) {
        let hasPerFileError = false;
        newInputs.forEach(input => {
            if (input.files && input.files[0]) {
                const file = input.files[0];
                if (isPdf(file.name) && file.size > EDIT_PDF_MAX_BYTES) {
                    hasPerFileError = true;
                }
                if (!isPdf(file.name) && file.size > EDIT_IMG_BLOCK_BYTES) {
                    hasPerFileError = true;
                }
            }
        });

        if (hasPerFileError) {
            alert('Please fix file size errors before submitting.');
            e.preventDefault();
            return false;
        }
    }
});

document.addEventListener("click", function (e) {
    const btn = e.target.closest(".cancel-leave-btn");
    if (!btn) return;

    const leaveId = btn.dataset.leaveId;

    if (!confirm("Are you sure you want to cancel this leave request?")) {
        return;
    }

    fetch(`/cancel_leave/${leaveId}`, {
        method: "POST",
        headers: {
            "X-Requested-With": "XMLHttpRequest"
        }
    })
    .then(res => res.json())
    .then(data => {
        if (data.success) {
            alert(data.message);
            location.reload();
        } else {
            alert(data.message);
        }
    })
    .catch(err => {
        console.error(err);
        alert("Something went wrong.");
    });
});