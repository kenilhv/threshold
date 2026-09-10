import { redirect } from "next/navigation";

import { readSession } from "@/lib/session";

export default async function Home() {
  const session = await readSession();
  redirect(session ? "/queue" : "/api/auth/login");
}
