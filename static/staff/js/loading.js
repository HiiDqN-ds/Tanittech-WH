// Global loading handler for links and buttons
function showLoading(btn, text = 'Loading...') {
  btn.disabled = true;
  btn.style.opacity = '0.7';
  btn.innerHTML = `<span class="flex items-center gap-2">
    <svg class="animate-spin -ml-1 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3"></circle>
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12c0-4.418 3.582-8 8-8a9.863 9.863 0 00 6.255 2.551l.065.517A5.998 5.998 0 0112 12c-3.309 0-6 2.691-6 6v.041z"></path>
    </svg>
    ${text}
  </span>`;
}

function handleDelete(event, link) {
  event.preventDefault();
  if (confirm('Are you sure you want to delete this ticket?')) {
    showLoading(link, 'Deleting...');
    setTimeout(() => {
      window.location.href = link.href;
    }, 500);
  }
}

document.addEventListener('DOMContentLoaded', function() {
  // All loading buttons and form submits
  document.querySelectorAll('.loading-btn, .loading-form button[type="submit"], .delete-link, .save-link').forEach(el => {
    el.addEventListener('click', function(e) {
      const loadingText = this.dataset.loading || 'Processing...';
      showLoading(this, loadingText);
      if (this.classList.contains('delete-link') || this.classList.contains('save-link')) {
        e.preventDefault();
        setTimeout(() => {
          window.location.href = this.href;
        }, 300);
      }
    });
  });
});

