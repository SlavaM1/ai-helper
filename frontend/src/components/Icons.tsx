import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement>

function Icon({ children, ...props }: IconProps) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{children}</svg>
}

export const PlusIcon = (props: IconProps) => <Icon {...props}><path d="M12 5v14M5 12h14" /></Icon>
export const MenuIcon = (props: IconProps) => <Icon {...props}><path d="M4 7h16M4 12h16M4 17h16" /></Icon>
export const CloseIcon = (props: IconProps) => <Icon {...props}><path d="m6 6 12 12M18 6 6 18" /></Icon>
export const SendIcon = (props: IconProps) => <Icon {...props}><path d="m21 3-7.5 18-3.8-7.7L2 9.5 21 3Z" /><path d="m9.7 13.3 4.6-4.6" /></Icon>
export const BranchIcon = (props: IconProps) => <Icon {...props}><circle cx="6" cy="5" r="2" /><circle cx="18" cy="6" r="2" /><circle cx="6" cy="19" r="2" /><path d="M6 7v10M8 12h4a6 6 0 0 0 6-4" /></Icon>
export const DatabaseIcon = (props: IconProps) => <Icon {...props}><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></Icon>
export const ChartIcon = (props: IconProps) => <Icon {...props}><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></Icon>
export const TrashIcon = (props: IconProps) => <Icon {...props}><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5" /></Icon>
export const EditIcon = (props: IconProps) => <Icon {...props}><path d="m4 20 4.2-1 10-10a2.8 2.8 0 0 0-4-4l-10 10L4 20ZM13 6l4 4" /></Icon>
export const CopyIcon = (props: IconProps) => <Icon {...props}><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></Icon>
export const CheckIcon = (props: IconProps) => <Icon {...props}><path d="m5 12 4 4L19 6" /></Icon>
export const ChevronIcon = (props: IconProps) => <Icon {...props}><path d="m8 10 4 4 4-4" /></Icon>
export const SparkIcon = (props: IconProps) => <Icon {...props}><path d="m12 3 1.3 4.1L17 9l-3.7 1.9L12 15l-1.3-4.1L7 9l3.7-1.9L12 3Z" /><path d="m19 15 .6 1.8L21 18l-1.4 1.2L19 21l-.6-1.8L17 18l1.4-1.2L19 15Z" /></Icon>
export const RefreshIcon = (props: IconProps) => <Icon {...props}><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 8.5A7 7 0 0 1 18 6l2 6M18 15.5A7 7 0 0 1 6 18l-2-6" /></Icon>
export const ExternalIcon = (props: IconProps) => <Icon {...props}><path d="M14 4h6v6M20 4l-9 9" /><path d="M18 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6" /></Icon>
export const AlertIcon = (props: IconProps) => <Icon {...props}><path d="M12 3 2.8 19h18.4L12 3Z" /><path d="M12 9v4M12 17h.01" /></Icon>
