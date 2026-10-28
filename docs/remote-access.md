# Using it from your phone or another computer

The app has no login, so don't open its port to your network. Reach it through a private network that only your own
devices are on, and put the name it's reached by in `REMOTE_HOSTS` in `.env`.

Everything works there except connecting a bank and **Log in again**, which work only on the computer running the app:
a bank's login on another device needs a secure redirect the app doesn't set up, and a login that breaks partway could
use up one of Plaid's 10 Trial connections.

## With Tailscale

1. Install [Tailscale](https://tailscale.com) on the computer running the app and on your phone, signed in to the same tailnet.
2. On the computer, run `tailscale serve --bg 8080`. It shares the app with your tailnet only, over HTTPS, until you run `tailscale serve reset`. `tailscale serve status` shows its address, like `https://my-computer.my-tailnet.ts.net`. Don't use `tailscale funnel`, which makes it public.
3. In `.env`, set `REMOTE_HOSTS=my-computer.my-tailnet.ts.net` (the address without `https://`), and run `docker compose up -d`.
4. Open the address on your phone.

The first time, Tailscale asks you to turn on HTTPS certificates. That publishes your devices' names in public certificate logs, so rename a device first if its name is something you'd rather keep private ([Tailscale's guide](https://tailscale.com/kb/1153/enabling-https)).

## With Surge Ponte

For devices that run [Surge](https://nssurge.com) and are signed in to the same iCloud account.

1. On the computer running the app, open Surge → **Overview**, turn on **Surge Ponte**, let it test the network, and give the computer a name. Below it's called `NAME`.
2. Turn on Surge Ponte on the other device.
3. In `.env`, set `REMOTE_HOSTS=*.sgponte`, and run `docker compose up -d`.
4. Open `http://NAME.sgponte:8080` on the other device.

Safari may call the page not secure: Ponte already encrypts the connection end to end, so the app doesn't use HTTPS. Away from home, Ponte needs a network that allows direct connections or a proxy that relays UDP; Surge's network test tells you which ([Surge's guide](https://kb.nssurge.com/surge-knowledge-base/guidelines/ponte)).
