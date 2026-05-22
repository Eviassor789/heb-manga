/**
 * /library → redirect to / (the library homepage)
 * This route exists for backward compatibility with any saved links.
 */
import { redirect } from 'next/navigation'

export default function LibraryRedirect() {
  redirect('/')
}
