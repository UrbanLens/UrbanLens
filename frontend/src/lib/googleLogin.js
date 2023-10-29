export async function googleLogin() {
  let auth2;
  gapi.load('auth2', () => {
    auth2 = gapi.auth2.init({
      client_id: 'YOUR_CLIENT_ID',
    });
  });

  return auth2.signIn().then(googleUser => {
    const id_token = googleUser.getAuthResponse().id_token;
    return id_token;
  });
}
