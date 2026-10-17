const form = document.getElementById('login-form');
const input = document.getElementById('password');
const button = document.getElementById('login-submit');
const message = document.getElementById('login-message');
let busy = false;
button.disabled = true;
fetch('/api/config').then(r => r.json()).then(config => {
  if (!config.password_required) {
    input.required = false; input.hidden = true;
    form.querySelector('label').hidden = true;
    button.textContent = '进入本地预览';
    message.textContent = '本地预览模式：未启用模型，不验证账号。';
  } else message.textContent = '请输入服务管理员提供的演示访问口令。';
  button.disabled = false;
}).catch(() => { message.textContent = '无法连接服务，请刷新重试。'; });
form.onsubmit = async event => {
  event.preventDefault();
  if (busy) return;
  busy = true; button.disabled = true;
  try {
    const response = await fetch('/api/login', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({password:input.value}), signal: AbortSignal.timeout(10000) });
    const result = await response.json(); input.value = '';
    if (!response.ok) throw new Error(result.error || '登录失败，请稍后重试。');
    location.replace('/chat');
  } catch (error) { message.textContent = error.message; busy = false; button.disabled = false; }
};
