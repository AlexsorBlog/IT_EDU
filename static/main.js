const chatForm = document.querySelector('.chat-section .chat-form');
chatForm?.classList.remove('is-hidden');

const dialogSection = document.querySelector('.dialog');
const dialogForm = dialogSection?.querySelector('.chat-form');

dialogSection?.addEventListener('click', e => {
  const button = e.target.closest('button[data-correct]');
  if (!button) return;

  if (button.dataset.correct === 'false') {
    dialogForm?.classList.remove('is-hidden');
    addClarifyingMessage();
    hideUserResponses();
  }
});

dialogForm?.addEventListener('submit', e => e.preventDefault());

function hideUserResponses() {
  const userResponses = dialogSection.querySelector('.user-responses');
  if (!userResponses) return;

  userResponses.classList.add('is-hidden');
  setTimeout(() => userResponses.remove(), 1000);
}

function addClarifyingMessage() {
  const container = dialogSection.querySelector('.dialog-wrapper');
  container?.insertAdjacentHTML('beforeend', getClarifyingMessageMarkup());
}

function getClarifyingMessageMarkup() {
  return `
    <div class="answer-wrapper">
      <p class="answer">Add more photos or explanation :)</p>
    </div>
  `;
}
