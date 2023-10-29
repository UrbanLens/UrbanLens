<script>
  import { onMount } from 'svelte';

  let auth2;

  onMount(() => {
    gapi.load('auth2', () => {
      auth2 = gapi.auth2.init({
        client_id: 'YOUR_CLIENT_ID',
      });
    });
  });

  const login = () => {
    auth2.signIn().then(googleUser => {
      const id_token = googleUser.getAuthResponse().id_token;
      // Send the ID token to your server
    });
  };
</script>

<div>
  <h1>Login</h1>
  <button on:click={login}>Login with Google</button>
</div>
