<script>
  let username = '';
  let password = '';

  const login = async () => {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ username, password })
    });

    if (response.ok) {
      // Redirect to home page
      window.location.href = '/';
    } else {
      // Show error message
      console.error('Login failed');
    }
  };
</script>

<div>
  <h1>Login</h1>
  <input type="text" bind:value={username} placeholder="Username" />
  <input type="password" bind:value={password} placeholder="Password" />
  <button on:click={login}>Login</button>
</div>
